"""Manim 数学动画通道：时间轴下发、合音轨、失败处理、请求分发。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import generation, manim_render

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")

SCRIPT = """
from manim import *
class MathLesson(Scene):
    def construct(self):
        self.wait(1)
"""


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "explain_math.py").write_text(SCRIPT, encoding="utf-8")
    return ws


def _one_second_video(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=1:size=320x240:rate=15", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out.strip())


def _fake_manim(settings, fake_video: Path, seen: dict, returncode: int = 0, log: str = "ok"):
    real_run = manim_render._run

    async def runner(*cmd, cwd, timeout, env=None):
        if str(cmd[0]) == settings.manim_bin:
            meta_path = Path(cwd) / "narration_meta.json"
            seen["meta"] = json.loads(meta_path.read_text(encoding="utf-8"))
            seen["cmd"] = list(cmd)
            if returncode == 0:
                media = Path(cmd[cmd.index("--media_dir") + 1])
                out = media / "videos" / "explain_math" / "720p30" / "manim_out.mp4"
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fake_video, out)
            return returncode, log
        return await real_run(*cmd, cwd=cwd, timeout=timeout, env=env)

    return runner


async def test_missing_script_and_scene_report_notices(workspace, settings):
    notices: list[str] = []
    assert await manim_render.render_math_video(
        workspace=workspace, settings=settings, request={}, notices=notices
    ) == []
    assert notices and "缺少 script" in notices[0]

    notices = []
    assert await manim_render.render_math_video(
        workspace=workspace, settings=settings,
        request={"script": "explain_math.py"}, notices=notices,
    ) == []
    assert notices and "scene" in notices[0]

    notices = []
    assert await manim_render.render_math_video(
        workspace=workspace, settings=settings,
        request={"script": "nope.py", "scene": "MathLesson"}, notices=notices,
    ) == []
    assert notices and "找不到脚本" in notices[0]


@needs_ffmpeg
async def test_render_writes_timeline_muxes_narration_and_pads_video(
    workspace, settings, tmp_path, monkeypatch
):
    """旁白 2 秒、画面 1 秒 → 成片补到 2 秒并带音轨；逐句时间轴要下发给脚本。"""
    fake_video = tmp_path / "raw.mp4"
    _one_second_video(fake_video)

    async def fake_tts(*, workspace, settings, request, notices, timeline_out=None):  # noqa: A002
        target = workspace / "narration.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=2", "-ar", "24000", "-ac", "1", str(target)],
            check=True,
        )
        if timeline_out is not None:
            timeline_out.append({"text": "第一句。", "start": 0.0, "end": 1.4})
            timeline_out.append({"text": "第二句。", "start": 1.9, "end": 2.0})
        return [target]

    monkeypatch.setattr(generation, "_synthesize_speech", fake_tts)
    seen: dict = {}
    monkeypatch.setattr(manim_render, "_run", _fake_manim(settings, fake_video, seen))

    produced = await manim_render.render_math_video(
        workspace=workspace, settings=settings,
        request={"script": "explain_math.py", "scene": "MathLesson", "output": "数学讲解.mp4",
                 "quality": "draft", "narration": {"text": "第一句。第二句。"}},
        notices=[],
    )
    names = [p.name for p in produced]
    assert "数学讲解.mp4" in names and any(n.startswith("讲解配音") for n in names)

    meta = seen["meta"]
    assert meta["duration"] == pytest.approx(2.0, abs=0.15)
    assert [s["text"] for s in meta["segments"]] == ["第一句。", "第二句。"]
    assert meta["segments"][1]["start"] == pytest.approx(1.9, abs=0.01)
    assert meta["resolution"] == [1280, 720]
    assert not (workspace / "narration_meta.json").exists(), "中间文件不该留在工作区"

    out = workspace / "数学讲解.mp4"
    assert _probe_duration(out) == pytest.approx(2.0, abs=0.25)
    streams = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "video" in streams and "audio" in streams


@needs_ffmpeg
async def test_render_failure_reports_notice(workspace, settings, tmp_path, monkeypatch):
    fake_video = tmp_path / "raw.mp4"
    _one_second_video(fake_video)
    seen: dict = {}
    monkeypatch.setattr(
        manim_render, "_run",
        _fake_manim(settings, fake_video, seen, returncode=1, log="LatexError: 公式写错了"),
    )
    notices: list[str] = []
    produced = await manim_render.render_math_video(
        workspace=workspace, settings=settings,
        request={"script": "explain_math.py", "scene": "MathLesson"}, notices=notices,
    )
    assert produced == []
    assert notices and "数学动画渲染失败" in notices[0]
    assert "LatexError" in notices[0]


@needs_ffmpeg
async def test_manim_request_file_is_dispatched_and_cleaned(
    workspace, settings, tmp_path, monkeypatch
):
    """`.manim_request.json` 要能被 process_requests 正确分发，且请求文件必删。"""
    fake_video = tmp_path / "raw.mp4"
    _one_second_video(fake_video)
    seen: dict = {}
    monkeypatch.setattr(manim_render, "_run", _fake_manim(settings, fake_video, seen))
    (workspace / generation.MANIM_REQUEST_FILE).write_text(
        json.dumps({"script": "explain_math.py", "scene": "MathLesson", "output": "数学讲解.mp4"}),
        encoding="utf-8",
    )

    result = await generation.process_requests(workspace, settings)

    assert [p.name for p in result.files] == ["数学讲解.mp4"]
    assert not (workspace / generation.MANIM_REQUEST_FILE).exists()
    assert seen["cmd"][1] == "render" and "--fps" in seen["cmd"]
