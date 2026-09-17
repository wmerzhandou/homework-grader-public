"""用 Manim 渲染"数学动画"讲解视频（真 LaTeX 公式排版、几何演示、推导过程）。

与其它两条视频路径的分工（提示词里也是这么写的）：
  - 一般讲解 / 图解 / 流程 / 口播            → HyperFrames（app/render.py）
  - **数学推导 / 公式 / 几何证明 / 数轴数形** → Manim（本模块）
  - 写实镜头 / 氛围素材                      → H3 农场（app/video_gen.py）

为什么由后端渲染：沙箱里没有网络，也没有 Manim/LaTeX 工具链；渲染器要跑 LaTeX、
ffmpeg、并且要把旁白音轨合进去，放在后端最稳。

请求文件 `.manim_request.json`（隐藏文件，不会变成产出物）：
{
  "script": "explain_math.py",     // 模型写的 Manim 脚本（会话工作区里）
  "scene": "FractionMeaning",      // 场景类名（脚本里的 class）
  "output": "分数的意义讲解.mp4",
  "quality": "draft" | "high",     // draft=1280×720@30，high=1920×1080@30
  "narration": {"text": "…", "voice": "default", "engine": "index_tts"|"mmx"}
}

**对轴的关键**：渲染前后端先合成旁白，并把逐句时间轴写成脚本同目录的
`narration_meta.json`（脚本按相对路径读，因为 manim 的 cwd 就是脚本目录）：
  {"duration": 23.73, "fps": 30, "resolution": [1920, 1080],
   "segments": [{"index": 0, "text": "…", "start": 0.83, "end": 3.21}, …]}
脚本用 `meta["segments"][i]["start"]` 决定每个动画什么时候出现（模板里给了
`wait_until()` 写法），这样"话没说完画面就切走"从机制上不会发生。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path

from .async_utils import notify as notify_progress
from .config import Settings

logger = logging.getLogger(__name__)

_QUALITY_PROFILES = {
    "draft": (1280, 720, 30),
    "high": (1920, 1080, 30),
}
_AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")


def _quality_profile(quality: str) -> tuple[int, int, int]:
    return _QUALITY_PROFILES.get(quality.strip().lower(), _QUALITY_PROFILES["draft"])


async def _probe(path: Path, settings: Settings, entries: str, key: str) -> float:
    """用 ffprobe 读一个数值（时长/采样率等），失败返回 0。"""
    ffprobe = settings.ffmpeg_bin.replace("ffmpeg", "ffprobe")
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-show_entries", entries, "-of", "default=nw=1:nk=1",
            str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return float(out.decode().strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return 0.0


async def _run(*cmd: str, cwd: Path, timeout: float, env: dict | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode, (out or b"").decode(errors="replace")


async def _mux_narration(
    *,
    video: Path,
    narration: Path | None,
    target: Path,
    settings: Settings,
    notices: list[str],
) -> bool:
    """把旁白合进 Manim 成片；成片比旁白短就冻结最后一帧补齐（画面不会被截断）。"""
    ffmpeg = settings.ffmpeg_bin
    if narration is None:
        shutil.copy2(video, target)
        return True

    video_duration = await _probe(video, settings, "format=duration", "duration")
    audio_duration = await _probe(narration, settings, "format=duration", "duration")
    pad = max(0.0, audio_duration - video_duration)
    if pad > 0.05:
        # 画面短了：把最后一帧冻住补时长，音频不动
        args = [
            "-y", "-v", "error", "-i", str(video), "-i", str(narration),
            "-filter_complex",
            f"[0:v]tpad=stop_mode=clone:stop_duration={pad:.3f}[v]",
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-shortest", str(target),
        ]
    else:
        # 画面够长：直接复制视频流 + 编音频，画面不重编码、不裁切
        args = [
            "-y", "-v", "error", "-i", str(video), "-i", str(narration),
            "-map", "0:v", "-map", "1:a", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "160k", str(target),
        ]
    code, log = await _run(ffmpeg, *args, cwd=target.parent, timeout=settings.render_timeout_s)
    if code != 0 or not target.is_file() or target.stat().st_size == 0:
        logger.warning("Manim 成片合音轨失败: %s", log[-300:])
        notices.append("⚠️ 旁白合入成片失败，已只交付无声成片。")
        shutil.copy2(video, target)
        return False
    return True


def _find_render(media_dir: Path, output_name: str) -> Path | None:
    """找 manim 渲染出来的成片（不同版本目录层级略有差别，按文件名兜底）。"""
    candidates = [p for p in media_dir.rglob(output_name) if p.is_file()]
    if not candidates:
        candidates = [
            p for p in media_dir.rglob("*.mp4")
            if p.is_file() and "partial_movie_files" not in p.parts
        ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


async def render_math_video(
    *,
    workspace: Path,
    settings: Settings,
    request: dict,
    notify=None,  # noqa: ANN001 - async callable(str) -> None
    notices: list[str],
) -> list[Path]:
    """执行一次 Manim 渲染；返回写回会话工作区的成片（以及旁白音频）。"""
    from . import generation  # 局部导入避免循环依赖

    script_name = str(request.get("script") or "").strip()
    scene = str(request.get("scene") or "").strip()
    if not script_name:
        notices.append("⚠️ Manim 渲染失败：`.manim_request.json` 里缺少 script。")
        return []
    script = (workspace / script_name).resolve()
    if not script.is_file() or not script.is_relative_to(workspace.resolve()):
        notices.append(f"⚠️ Manim 渲染失败：找不到脚本「{script_name}」。")
        return []
    if not scene:
        notices.append("⚠️ Manim 渲染失败：需要在 `.manim_request.json` 里写 scene（场景类名）。")
        return []

    output_name = str(request.get("output") or "数学讲解.mp4").strip()
    if not output_name.lower().endswith(".mp4"):
        output_name = f"{Path(output_name).stem}.mp4"
    width, height, fps = _quality_profile(str(request.get("quality") or "draft"))

    work_dir = settings.data_dir / "manim" / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)
    produced: list[Path] = []
    try:
        # 1) 旁白（先定声音，再让脚本按时间轴排画面）
        narration_path: Path | None = None
        timeline: list[dict] = []
        narration_request = request.get("narration")
        audio_name = str(request.get("audio") or "").strip()
        if isinstance(narration_request, dict) and str(narration_request.get("text") or "").strip():
            await notify_progress(notify, "正在合成旁白…")
            tts_request = {
                "text": narration_request.get("text"),
                "filename": "narration.mp3",
                "voice": narration_request.get("voice") or settings.narration_voice,
                "engine": narration_request.get("engine") or settings.narration_engine,
                "speed": narration_request.get("speed") or settings.narration_speed,
                "pace": narration_request.get("pace") or "narration",
            }
            if isinstance(narration_request.get("pauses"), dict):
                tts_request["pauses"] = narration_request["pauses"]
            made = await generation._synthesize_speech(  # noqa: SLF001 - 复用同一套 TTS 逻辑
                workspace=work_dir,
                settings=settings,
                request=tts_request,
                notices=notices,
                timeline_out=timeline,
            )
            if made:
                suffix = made[0].suffix or ".mp3"
                kept = workspace / f"讲解配音{suffix}"
                shutil.copy2(made[0], kept)
                produced.append(kept)
                narration_path = kept
        elif audio_name:
            candidate = (workspace / audio_name).resolve()
            if candidate.is_file() and candidate.is_relative_to(workspace.resolve()):
                narration_path = candidate

        duration = 0.0
        if narration_path is not None:
            duration = await _probe(narration_path, settings, "format=duration", "duration")

        # 2) 写时间轴给脚本读（manim 的 cwd = 脚本目录，所以放脚本同目录）
        meta_path = script.parent / "narration_meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "duration": round(duration, 3),
                    "fps": fps,
                    "resolution": [width, height],
                    "segments": [
                        {"index": i, **segment} for i, segment in enumerate(timeline)
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # 3) 渲染（Manim 自己找 ffmpeg/latex/dvisvgm，都在 PATH 里）
        await notify_progress(notify, f"正在渲染数学动画（{width}×{height}）…")
        command = [
            settings.manim_bin, "render",
            "-r", f"{width},{height}", "--fps", str(fps),
            "--media_dir", str(work_dir / "media"),
            "-o", "manim_out.mp4",
            str(script), scene,
        ]
        code, log = await _run(
            *command,
            cwd=script.parent,
            timeout=settings.manim_timeout_s,
            env=None,
        )
        rendered = _find_render(work_dir / "media", "manim_out.mp4")
        if code != 0 or rendered is None:
            tail = [line for line in log.strip().splitlines() if line.strip()]
            reason = tail[-1][:200] if tail else "未知错误"
            if code == 124:
                reason = f"渲染超时（超过 {settings.manim_timeout_s:.0f} 秒）"
            logger.warning("Manim 渲染失败: %s", log[-500:])
            notices.append(f"⚠️ 数学动画渲染失败：{reason}")
            return produced

        # 4) 合音轨并写回工作区
        target = workspace / output_name
        await _mux_narration(
            video=rendered,
            narration=narration_path,
            target=target,
            settings=settings,
            notices=notices,
        )
        # 默认 720p：draft 本来就是 720p，这里主要是兜住"模型自己写了更大的分辨率"
        from .render import cap_video_height  # 局部导入，避免循环依赖

        await cap_video_height(target, settings, str(request.get("quality") or "draft").lower())
        meta_path.unlink(missing_ok=True)  # 中间产物不进产出物清单
        produced.append(target)
        return produced
    except FileNotFoundError:
        logger.warning("Manim 渲染缺少文件（CLI 或素材）", exc_info=True)
        notices.append("⚠️ 数学动画渲染失败：服务器上缺少 Manim CLI。")
        return produced
    except Exception:  # noqa: BLE001 - 渲染失败不影响主流程
        logger.warning("Manim 渲染异常", exc_info=True)
        notices.append("⚠️ 数学动画渲染失败：内部错误。")
        return produced
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
