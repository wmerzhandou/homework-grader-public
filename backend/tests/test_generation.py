"""模型发起的生成请求：语音（IndexTTS）与图片（mmx）的执行、失败处理与请求文件清理。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import generation
from app.config import get_settings


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _write_request(ws: Path, name: str, payload: dict) -> Path:
    path = ws / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 语音
# --------------------------------------------------------------------------
async def test_tts_request_generates_wav_and_cleans_request(workspace, settings, monkeypatch):
    audio = b"RIFFfake-wav-bytes"
    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):  # noqa: A002
            captured["url"] = url
            captured["json"] = json
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"success": True, "audio_base64": base64.b64encode(audio).decode()},
            )

    monkeypatch.setattr(generation.httpx, "AsyncClient", _FakeClient)
    request_path = _write_request(
        workspace, generation.TTS_REQUEST_FILE, {"text": "你好，这是一段测试朗读。", "filename": "朗读.wav"}
    )

    result = await generation.process_requests(workspace, settings)

    assert result.notices == []
    assert [p.name for p in result.files] == ["朗读.wav"]
    assert (workspace / "朗读.wav").read_bytes() == audio
    assert not request_path.exists(), "请求文件必须被清理，避免下一轮重复执行"
    assert captured["url"].endswith("/api/preview")
    assert captured["json"]["tts_engine"] == "index_tts"
    assert "你好" in captured["json"]["text"]


async def test_tts_service_failure_reports_notice(workspace, settings, monkeypatch):
    class _FailingClient:
        def __init__(self, **kwargs): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):  # noqa: A002
            return SimpleNamespace(status_code=502, json=lambda: {"success": False, "error": "模型未就绪"})

    monkeypatch.setattr(generation.httpx, "AsyncClient", _FailingClient)
    request_path = _write_request(workspace, generation.TTS_REQUEST_FILE, {"text": "测试"})

    result = await generation.process_requests(workspace, settings)

    assert result.files == []
    assert result.notices and "语音生成失败" in result.notices[0]
    assert not request_path.exists()


# --------------------------------------------------------------------------
# 图片
# --------------------------------------------------------------------------
async def test_image_request_invokes_mmx_and_fixes_extension(workspace, settings, monkeypatch):
    """mmx 实际输出 JPEG：即使文件名写成 .png 也要纠正扩展名。"""
    monkeypatch.setattr(settings.__class__, "mmx_bin", property(lambda self: "/usr/bin/true"), raising=False)
    called: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"{}", b""

    async def fake_exec(*cmd, **kwargs):
        called["cmd"] = list(cmd)
        out_index = list(cmd).index("--out") + 1
        target = Path(cmd[out_index])
        target.write_bytes(b"\xff\xd8\xff\xe0FAKE-JPEG")
        return _FakeProc()

    monkeypatch.setattr(generation.asyncio, "create_subprocess_exec", fake_exec)
    request_path = _write_request(
        workspace,
        generation.IMAGE_REQUEST_FILE,
        {"prompt": "一只戴帽子的猫", "filename": "插图.png", "aspect_ratio": "1:1"},
    )

    result = await generation.process_requests(workspace, settings)

    assert result.notices == []
    assert [p.name for p in result.files] == ["插图.jpg"]
    assert (workspace / "插图.jpg").is_file()
    assert not (workspace / "插图.png").exists()
    assert called["cmd"][1:3] == ["image", "generate"]
    assert "--non-interactive" in called["cmd"]
    assert not request_path.exists()


async def test_image_failure_reports_notice(workspace, settings, monkeypatch):
    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"quota exceeded\n"

    async def fake_exec(*cmd, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(generation.asyncio, "create_subprocess_exec", fake_exec)
    request_path = _write_request(workspace, generation.IMAGE_REQUEST_FILE, {"prompt": "测试"})

    result = await generation.process_requests(workspace, settings)

    assert result.files == []
    assert any("图片生成失败" in n and "quota exceeded" in n for n in result.notices)
    assert not request_path.exists()


# --------------------------------------------------------------------------
# 健壮性
# --------------------------------------------------------------------------
async def test_bad_json_request_reports_and_removes(workspace, settings):
    path = workspace / generation.TTS_REQUEST_FILE
    path.write_text("{not json", encoding="utf-8")
    result = await generation.process_requests(workspace, settings)
    assert result.files == []
    assert result.notices and "解析失败" in result.notices[0]
    assert not path.exists()


async def test_empty_request_is_ignored(workspace, settings):
    result = await generation.process_requests(workspace, settings)
    assert result.files == [] and result.notices == []


# --------------------------------------------------------------------------
# 视频（H3 农场 / ComfyUI）
# --------------------------------------------------------------------------
class _FakeComfy:
    """按 URL 分发的假 ComfyUI：/prompt → /history → /view。"""

    def __init__(self, *, video_bytes=b"FAKE-MP4", upload_name="ref.png", fail_prompt=False):
        self.video_bytes = video_bytes
        self.upload_name = upload_name
        self.fail_prompt = fail_prompt
        self.prompt_payload: dict | None = None
        self.uploaded: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, files=None, data=None):  # noqa: A002
        if url.endswith("/upload/image"):
            self.uploaded = {"files": files, "data": data}
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"name": self.upload_name, "subfolder": ""},
            )
        if url.endswith("/prompt"):
            self.prompt_payload = json
            if self.fail_prompt:
                return SimpleNamespace(status_code=400, text="bad workflow", json=lambda: {})
            return SimpleNamespace(status_code=200, json=lambda: {"prompt_id": "pid-1"})
        raise AssertionError(f"unexpected POST {url}")

    async def get(self, url, params=None):
        if url.endswith("/history/pid-1"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "pid-1": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "92": {
                                "images": [
                                    {"filename": "MiniMax_H3_00001_.mp4", "subfolder": "video", "type": "output"}
                                ],
                                "animated": [True],
                            }
                        },
                    }
                },
            )
        if url.endswith("/view"):
            assert params and params.get("subfolder") == "video"
            return SimpleNamespace(status_code=200, content=self.video_bytes)
        raise AssertionError(f"unexpected GET {url}")


async def test_video_request_runs_h3_workflow(workspace, settings, monkeypatch):
    fake = _FakeComfy()
    monkeypatch.setattr(generation.video_gen.httpx, "AsyncClient", lambda **kw: fake)
    monkeypatch.setattr(generation.asyncio, "sleep", lambda *_: _instant())

    request_path = _write_request(
        workspace,
        generation.VIDEO_REQUEST_FILE,
        {"prompt": "小狗在沙滩上奔跑，海浪声", "filename": "沙滩.mp4", "seconds": 5, "resolution": "480p"},
    )
    statuses: list[str] = []

    async def notify(message: str) -> None:
        statuses.append(message)

    result = await generation.process_requests(workspace, settings, notify=notify)

    assert result.notices == []
    assert [p.name for p in result.files] == ["沙滩.mp4"]
    assert (workspace / "沙滩.mp4").read_bytes() == b"FAKE-MP4"
    assert not request_path.exists()
    # 工作流被正确改写：提示词/分辨率/时长
    node_131 = fake.prompt_payload["prompt"]["131"]["inputs"]
    assert node_131["prompt"] == "小狗在沙滩上奔跑，海浪声"
    assert (node_131["width"], node_131["height"]) == (864, 480)
    assert fake.prompt_payload["prompt"]["133"]["inputs"]["value"] == 5
    assert any("已提交" in s for s in statuses), "应通过回到前端的状态消息告知进度"


async def test_video_request_with_reference_uploads_image(workspace, settings, monkeypatch):
    fake = _FakeComfy(upload_name="上传的参考图.png")
    monkeypatch.setattr(generation.video_gen.httpx, "AsyncClient", lambda **kw: fake)
    monkeypatch.setattr(generation.asyncio, "sleep", lambda *_: _instant())
    ref = workspace / "参考图.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")

    _write_request(
        workspace,
        generation.VIDEO_REQUEST_FILE,
        {"prompt": "让这张图动起来", "filename": "动起来.mp4", "reference": "参考图.png", "seconds": 3},
    )
    result = await generation.process_requests(workspace, settings)

    assert result.notices == []
    assert fake.uploaded is not None, "参考图应被上传到 ComfyUI"
    assert fake.prompt_payload["prompt"]["137"]["inputs"]["image"] == "上传的参考图.png"
    assert (workspace / "动起来.mp4").is_file()


async def test_video_request_failure_reports_notice(workspace, settings, monkeypatch):
    fake = _FakeComfy(fail_prompt=True)
    monkeypatch.setattr(generation.video_gen.httpx, "AsyncClient", lambda **kw: fake)
    request_path = _write_request(
        workspace, generation.VIDEO_REQUEST_FILE, {"prompt": "测试", "seconds": 3}
    )

    result = await generation.process_requests(workspace, settings)

    assert result.files == []
    assert any("视频生成失败" in n for n in result.notices)
    assert not request_path.exists()


async def test_video_request_missing_reference_reports_notice(workspace, settings):
    request_path = _write_request(
        workspace,
        generation.VIDEO_REQUEST_FILE,
        {"prompt": "让它动", "reference": "不存在的图.png"},
    )
    result = await generation.process_requests(workspace, settings)
    assert result.files == []
    assert any("找不到参考图" in n for n in result.notices)
    assert not request_path.exists()


async def test_mmx_voice_engine_invokes_cli(workspace, settings, monkeypatch):
    """engine=mmx 时走 mmx CLI，可选音色/情感。"""
    called: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            out_index = called["cmd"].index("--out") + 1
            Path(called["cmd"][out_index]).write_bytes(b"ID3fake-mp3")
            return b"", b""

    async def fake_exec(*cmd, **kwargs):
        called["cmd"] = list(cmd)
        return _FakeProc()

    monkeypatch.setattr(generation.asyncio, "create_subprocess_exec", fake_exec)
    request_path = _write_request(
        workspace,
        generation.TTS_REQUEST_FILE,
        {
            "text": "你好呀",
            "filename": "甜妹朗读.mp3",
            "engine": "mmx",
            "voice": "female-tianmei",
            "emotion": "happy",
        },
    )
    result = await generation.process_requests(workspace, settings)

    assert result.notices == []
    assert [p.name for p in result.files] == ["甜妹朗读.mp3"]
    assert called["cmd"][1:3] == ["speech", "synthesize"]
    assert "female-tianmei" in called["cmd"] and "happy" in called["cmd"]
    assert not request_path.exists()


async def _instant():
    return None
