"""讲解视频渲染：工程组装（本地 GSAP / 时长改写 / 自动挂音轨）、失败处理。"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from app import generation, render


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


class _FakeProc:
    def __init__(
        self,
        project: Path,
        *,
        returncode: int = 0,
        output: bool = True,
        record: dict | None = None,
    ) -> None:
        self.project = project
        self.returncode = returncode
        self.output = output
        self.record = record

    async def communicate(self):
        if self.output:
            index = (self.project / "index.html").read_text(encoding="utf-8")
            if self.record is not None:
                # 组装目录稍后会被清理，所以在这里把内容记下来供断言
                self.record["assembled_html"] = index
                self.record["has_local_gsap"] = (self.project / "assets" / "gsap.min.js").is_file()
            (self.project.parent / "out.mp4").write_bytes(b"FAKE-MP4")
        return b"rendered ok", b""

    def kill(self):
        pass


@pytest.fixture()
def fake_cli(settings, monkeypatch):
    """拦截 hyperframes CLI，记录命令并产出假成片；不需要真渲染。"""
    calls: list[dict] = []

    async def fake_exec(*cmd, **kwargs):
        cwd = Path(kwargs.get("cwd", "."))
        record = {"cmd": list(cmd), "cwd": cwd}
        calls.append(record)
        return _FakeProc(cwd, record=record)

    async def fake_probe(path, _settings):
        return 15.33

    monkeypatch.setattr(render.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(render, "_probe_duration", fake_probe)
    return calls


def _composition(ws: Path, *, cdn: bool = True, duration: str = "20") -> Path:
    gsap = (
        "https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"
        if cdn
        else "assets/gsap.min.js"
    )
    path = ws / "explain.html"
    path.write_text(
        f"""<!doctype html><html><head><script src="{gsap}"></script></head>
<body>
  <div id="root" data-composition-id="main" data-start="0" data-duration="{duration}"
       data-width="1920" data-height="1080">
    <h1 class="clip" data-start="0" data-duration="{duration}" data-track-index="0">标题</h1>
    <!-- <audio id="narration" src="assets/narration.mp3"></audio> -->
  </div>
  <script>window.__timelines["main"] = gsap.timeline({{paused: true}});</script>
</body></html>""",
        encoding="utf-8",
    )
    return path


async def test_render_assembles_project_and_returns_output(workspace, settings, fake_cli, monkeypatch):
    _composition(workspace)
    narration = workspace / "旁白.wav"
    narration.write_bytes(b"RIFFfake-wav")

    async def fake_tts(*, workspace, settings, request, notices):  # noqa: A002
        target = workspace / "narration.wav"
        target.write_bytes(b"RIFFfake-wav")
        return [target]

    monkeypatch.setattr(generation, "_synthesize_speech", fake_tts)
    result = await render.render_composition(
        workspace=workspace,
        settings=settings,
        request={
            "composition": "explain.html",
            "output": "讲解.mp4",
            "quality": "draft",
            "narration": {"text": "先看第一题……"},
        },
        notices=[],
    )

    names = [p.name for p in result]
    assert "讲解.mp4" in names and "讲解配音.wav" in names
    assert (workspace / "讲解.mp4").read_bytes() == b"FAKE-MP4"

    # 组装后的工程：CDN 换成 本地 GSAP、时长按旁白改写、自动挂上音轨
    assembled = fake_cli[0]["assembled_html"]
    assert 'src="assets/gsap.min.js"' in assembled
    assert "cdn.jsdelivr.net" not in assembled
    assert 'data-duration="15.33"' in assembled and 'data-duration="20"' not in assembled
    assert "<audio" in assembled and 'src="assets/narration.wav"' in assembled
    assert fake_cli[0]["has_local_gsap"] is True

    # 时长以变量形式传给组合（JS 侧用它排时间轴）
    cmd = fake_cli[0]["cmd"]
    assert "--variables" in cmd
    variables = json.loads(cmd[cmd.index("--variables") + 1])
    assert variables["narrationDuration"] == 15.33


async def test_narration_voice_comes_from_server_settings(workspace, settings, fake_cli, monkeypatch):
    """默认音色/语速由服务端配置决定（换音色不动代码），模型显式指定时才覆盖。"""
    _composition(workspace)
    captured: list[dict] = []

    async def fake_tts(*, workspace, settings, request, notices):  # noqa: A002
        captured.append(dict(request))
        target = workspace / "narration.wav"
        target.write_bytes(b"RIFFfake-wav")
        return [target]

    monkeypatch.setattr(generation, "_synthesize_speech", fake_tts)
    settings = dataclasses.replace(
        settings, narration_engine="mmx", narration_voice="female-chengshu", narration_speed=0.75
    )

    await render.render_composition(
        workspace=workspace, settings=settings,
        request={"composition": "explain.html", "output": "a.mp4",
                 "narration": {"text": "先看第一题。"}},
        notices=[],
    )
    assert captured[0]["engine"] == "mmx"
    assert captured[0]["voice"] == "female-chengshu"
    assert captured[0]["speed"] == 0.75
    assert captured[0]["pace"] == "narration"

    await render.render_composition(
        workspace=workspace, settings=settings,
        request={"composition": "explain.html", "output": "b.mp4",
                 "narration": {"text": "换回默认音色。", "engine": "index_tts",
                               "voice": "default", "speed": 0.9}},
        notices=[],
    )
    assert captured[1]["engine"] == "index_tts" and captured[1]["speed"] == 0.9


async def test_render_without_narration_keeps_given_duration(workspace, settings, fake_cli):
    _composition(workspace, duration="30")
    result = await render.render_composition(
        workspace=workspace,
        settings=settings,
        request={"composition": "explain.html", "output": "无旁白.mp4", "duration": 30},
        notices=[],
    )
    assert (workspace / "无旁白.mp4").is_file()
    assembled = fake_cli[0]["assembled_html"]
    without_comments = re.sub(r"<!--.*?-->", "", assembled, flags=re.DOTALL)
    assert "<audio" not in without_comments, "没有旁白时不该凭空插入音轨"
    assert len(result) == 1


async def test_render_missing_composition_reports_notice(workspace, settings, fake_cli):
    notices: list[str] = []
    result = await render.render_composition(
        workspace=workspace,
        settings=settings,
        request={"composition": "不存在.html", "output": "x.mp4"},
        notices=notices,
    )
    assert result == [] and notices and "找不到组合文件" in notices[0]
    assert fake_cli == [], "找不到组合时不该调用 CLI"


async def test_render_cli_failure_reports_notice(workspace, settings, monkeypatch):
    _composition(workspace)

    async def failing_exec(*cmd, **kwargs):
        return _FakeProc(Path(kwargs["cwd"]), returncode=1, output=False)

    monkeypatch.setattr(render.asyncio, "create_subprocess_exec", failing_exec)
    notices: list[str] = []
    result = await render.render_composition(
        workspace=workspace,
        settings=settings,
        request={"composition": "explain.html", "output": "失败.mp4"},
        notices=notices,
    )
    assert result == [] and notices and "渲染失败" in notices[0]


async def test_render_request_runs_through_process_requests(workspace, settings, fake_cli):
    """`.render_request.json` 会被 process_requests 当成第四类生成任务执行。"""
    _composition(workspace)
    (workspace / generation.RENDER_REQUEST_FILE).write_text(
        json.dumps(
            {"composition": "explain.html", "output": "讲解.mp4", "duration": 12},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = await generation.process_requests(workspace, settings)
    assert [p.name for p in result.files] == ["讲解.mp4"]
    assert not (workspace / generation.RENDER_REQUEST_FILE).exists()
