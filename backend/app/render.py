"""讲解视频渲染：把模型写的 HTML 动画（HyperFrames 组合）渲染成成片。

为什么由后端渲染而不是让模型在沙箱里跑 CLI：
1) 沙箱没有网络，而工程脚手架默认从 CDN 引 GSAP、`npx hyperframes` 也可能要联网；
2) 后端有完整的 CLI + 无头 Chrome + ffmpeg，渲染稳定、可控超时。

请求文件 `.render_request.json`（隐藏文件，不会变成产出物）：
{
  "composition": "explain.html",        // 模型写的组合文件（放在会话工作区根下）
  "output": "讲解视频.mp4",
  "quality": "draft" | "high",           // draft≈15s/10s 素材，high 用于定稿
  "narration": {"text": "…", "voice": "default", "engine": "index_tts"|"mmx"},  // 可选：先合成旁白
  "audio": "已有的音频文件名",             // 可选：直接用工作区里已有的音频当旁白
  "duration": 53.2                       // 可选：不填则用旁白时长（有旁白时）
}

渲染时会把旁白放进 `assets/narration.*`，并以变量 `narrationDuration` 传给组合，
组合只需要声明该变量并在 JS 里读取，就能做到"画面随旁白时长"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from pathlib import Path

from .async_utils import notify as notify_progress
from .config import Settings
from .models import MaterialKind  # noqa: F401  (仅为类型提示可读性保留)

logger = logging.getLogger(__name__)

_CDN_GSAP_RE = re.compile(
    r"""https?://[^"']*gsap[^"']*\.js""", re.IGNORECASE
)
_AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
_ROOT_TAG_RE = re.compile(r"<div[^>]*data-composition-id=\"[^\"]+\"[^>]*>", re.IGNORECASE)
_ROOT_DURATION_RE = re.compile(
    r"(data-composition-id=\"[^\"]+\"[^>]*?data-duration=\")([0-9.]+)(\")", re.IGNORECASE
)


def _audio_suffix(path: Path) -> str:
    """按魔数判断音频真实格式（本地 TTS 返回的是 wav，不能只按文件名判断）。"""
    try:
        head = path.read_bytes()[:12]
    except OSError:
        return path.suffix.lower() or ".wav"
    if head.startswith(b"RIFF"):
        return ".wav"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if head[4:8] == b"ftyp":
        return ".m4a"
    return path.suffix.lower() or ".wav"


def _apply_duration(html: str, duration: float | None) -> str:
    """把根节点声明的时长（以及所有同值的 clip 时长）改成真实时长。

    渲染时长由**静态 HTML** 里的 `data-duration` 决定（脚本里改 DOM 来不及），
    所以必须在渲染前替换。以根节点原值为基准整体替换，clip 一并跟上，避免中途消失。
    """
    if not duration:
        return html
    match = _ROOT_DURATION_RE.search(html)
    if not match:
        return html
    original = match.group(2)
    return html.replace(f'data-duration="{original}"', f'data-duration="{duration:g}"')


def _ensure_narration_element(html: str, src: str, duration: float | None) -> str:
    """组合里没写 <audio> 时自动补上（必须是根节点的直接子元素，框架才会播放）。"""
    # 先去掉 HTML 注释再判断，否则骨架里那行"示例注释"会被误认为已经有 audio
    without_comments = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    if re.search(r"<audio\b", without_comments, re.IGNORECASE):
        return html
    root = _ROOT_TAG_RE.search(html)
    if not root:
        return html
    seconds = f"{duration:g}" if duration else "999"
    element = (
        f'\n      <audio id="narration" src="{src}" data-start="0" '
        f'data-duration="{seconds}" data-track-index="8"></audio>'
    )
    return html[: root.end()] + element + html[root.end():]


def template_dir(settings: Settings) -> Path:
    return settings.hf_template_dir


async def _probe_duration(path: Path, settings: Settings) -> float | None:
    ffprobe = settings.ffmpeg_bin.replace("ffmpeg", "ffprobe")
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except (ValueError, AttributeError):
        return None


async def _probe_height(path: Path, settings: Settings) -> int:
    try:
        ffprobe = settings.ffmpeg_bin.replace("ffmpeg", "ffprobe")
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=height", "-of", "default=nw=1:nk=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return int(out.decode().strip())
    except Exception:  # noqa: BLE001 - 探测失败就当"未知"，绝不影响成片交付
        return 0


async def cap_video_height(path: Path, settings: Settings, quality: str) -> None:
    """默认把成片压到 `VIDEO_MAX_HEIGHT`（默认 720p）：够看、文件小、传得快。

    只有请求里显式写了 `quality: "high"`（用户要高清）才保留原始分辨率。
    压缩失败不影响交付——保留原片即可。
    """
    limit = settings.video_max_height
    if quality == "high" or limit <= 0:
        return
    height = await _probe_height(path, settings)
    if not height or height <= limit:
        return
    temp = path.with_name(f"{path.stem}.capped{path.suffix}")
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.ffmpeg_bin, "-y", "-v", "error", "-i", str(path),
            "-vf", f"scale=-2:{limit}", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "copy", str(temp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
    except Exception:  # noqa: BLE001 - 压缩失败就保留原片
        temp.unlink(missing_ok=True)
        logger.warning("成片缩到 %sp 失败（调用异常），保留原分辨率", limit, exc_info=True)
        return
    if proc.returncode == 0 and temp.is_file() and temp.stat().st_size > 0:
        shutil.move(str(temp), str(path))
    else:
        temp.unlink(missing_ok=True)
        logger.warning("成片缩到 %sp 失败，保留原分辨率: %s", limit, (out or b"").decode()[-200:])


def _prepare_project_dir(
    settings: Settings,
    work_dir: Path,
    composition: Path,
    *,
    duration: float | None = None,
    narration_rel: str | None = None,
) -> Path:
    """搭一个最小 HyperFrames 工程：模板配置 + 模型的组合 + 本地资源。"""
    project = work_dir / "project"
    project.mkdir(parents=True, exist_ok=True)
    assets = project / "assets"
    assets.mkdir(exist_ok=True)

    template = template_dir(settings)
    shutil.copy2(template / "hyperframes.json", project / "hyperframes.json")
    shutil.copy2(template / "assets" / "gsap.min.js", assets / "gsap.min.js")
    (project / "meta.json").write_text(
        json.dumps({"id": "homework-explainer", "name": "homework-explainer"}, ensure_ascii=False),
        encoding="utf-8",
    )

    html = composition.read_text(encoding="utf-8")
    # 组合里若引了 CDN 的 GSAP，改成本地副本（渲染环境不联网）
    html = _CDN_GSAP_RE.sub("assets/gsap.min.js", html)
    # 时长必须以静态 HTML 为准；旁白存在时再自动补一个 <audio> 元素
    html = _apply_duration(html, duration)
    if narration_rel:
        html = _ensure_narration_element(html, narration_rel, duration)
    (project / "index.html").write_text(html, encoding="utf-8")
    return project


async def render_composition(
    *,
    workspace: Path,
    settings: Settings,
    request: dict,
    notify=None,  # noqa: ANN001 - async callable(str) -> None
    notices: list[str],
) -> list[Path]:
    """执行一次讲解视频渲染；返回写回会话工作区的成片（以及旁白音频）。"""
    composition_name = str(request.get("composition") or "").strip()
    if not composition_name:
        notices.append("⚠️ 渲染失败：`.render_request.json` 里缺少 composition 文件名。")
        return []
    composition = (workspace / composition_name).resolve()
    if not composition.is_file() or not composition.is_relative_to(workspace.resolve()):
        notices.append(f"⚠️ 渲染失败：找不到组合文件「{composition_name}」。")
        return []
    output_name = str(request.get("output") or "讲解视频.mp4").strip()
    if not output_name.lower().endswith(".mp4"):
        output_name = f"{Path(output_name).stem}.mp4"
    quality = str(request.get("quality") or "draft").strip().lower()
    if quality not in {"draft", "high"}:
        quality = "draft"

    work_dir = settings.data_dir / "render" / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)  # 旁白会先落到这里，必须在合成前建好
    produced: list[Path] = []
    try:
        # 旁白：优先用请求里的文本合成，其次用工作区已有的音频文件
        narration_path: Path | None = None
        narration_rel: str | None = None
        narration_request = request.get("narration")
        audio_name = str(request.get("audio") or "").strip()
        if isinstance(narration_request, dict) and str(narration_request.get("text") or "").strip():
            from . import generation  # 局部导入避免循环依赖

            await notify_progress(notify, "正在合成旁白…")
            tts_request = {
                "text": narration_request.get("text"),
                "filename": "narration.mp3",
                # 默认音色来自服务端配置（NARRATION_VOICE / NARRATION_ENGINE），
                # 模型在请求里显式给 voice/engine 时以它为准
                "voice": narration_request.get("voice") or settings.narration_voice,
                "engine": narration_request.get("engine") or settings.narration_engine,
                # 讲解类默认"慢一档 + 句末留白"（pace=narration）；模型显式给
                # speed/pauses 时以它为准，方便它按内容调节奏。
                "pace": narration_request.get("pace") or "narration",
                # 语速默认取服务端配置（换音色时一起调），模型显式给 speed 则覆盖
                "speed": narration_request.get("speed") or settings.narration_speed,
            }
            if isinstance(narration_request.get("pauses"), dict):
                tts_request["pauses"] = narration_request["pauses"]
            made = await generation._synthesize_speech(  # noqa: SLF001 - 复用同一套 TTS 逻辑
                workspace=work_dir, settings=settings, request=tts_request, notices=notices
            )
            if made:
                narration_path = made[0]
                suffix = _audio_suffix(narration_path)
                kept = workspace / f"讲解配音{suffix}"
                shutil.copy2(narration_path, kept)
                produced.append(kept)
                narration_path = kept
        elif audio_name:
            candidate = (workspace / audio_name).resolve()
            if candidate.is_file() and candidate.is_relative_to(workspace.resolve()):
                narration_path = candidate

        if narration_path is not None:
            narration_rel = f"assets/narration{_audio_suffix(narration_path)}"
            duration = await _probe_duration(narration_path, settings)
        else:
            duration = None
            try:
                requested = request.get("duration")
                duration = float(requested) if requested is not None else None
            except (TypeError, ValueError):
                duration = None

        variables: dict[str, object] = {}
        if duration:
            # 组合里声明 narrationDuration 后即可用它排时间轴/设置 data-duration
            variables["narrationDuration"] = round(duration, 3)

        await notify_progress(notify, f"正在渲染讲解视频（{quality}）…")
        # 组装工程放在旁白/时长都确定之后：静态 HTML 里的 data-duration 要按真实时长改写
        project = _prepare_project_dir(
            settings,
            work_dir,
            composition,
            duration=duration,
            narration_rel=narration_rel,
        )
        if narration_path is not None and narration_rel is not None:
            shutil.copy2(narration_path, project / narration_rel)
        cmd = [
            settings.hyperframes_bin, "render",
            "--quality", quality,
            "--output", str(work_dir / "out.mp4"),
        ]
        if variables:
            cmd += ["--variables", json.dumps(variables, ensure_ascii=False)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(project),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=settings.render_timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            notices.append(f"⚠️ 渲染超时（超过 {settings.render_timeout_s:.0f} 秒）。")
            return produced
        tail = (out or b"").decode(errors="replace")[-400:]
        rendered = work_dir / "out.mp4"
        if proc.returncode != 0 or not rendered.is_file() or rendered.stat().st_size == 0:
            logger.warning("hyperframes 渲染失败: %s", tail)
            notices.append(f"⚠️ 渲染失败：{tail.strip().splitlines()[-1] if tail.strip() else '未知错误'}")
            return produced
        target = workspace / output_name
        shutil.copy2(rendered, target)
        # 默认压到 720p（用户明确要高清时 quality=high 不压）
        await cap_video_height(target, settings, quality)
        produced.append(target)
        return produced
    except FileNotFoundError:
        logger.warning("渲染缺少文件（CLI 或素材）", exc_info=True)
        notices.append("⚠️ 渲染失败：服务器上缺少 hyperframes CLI 或素材文件。")
        return produced
    except Exception:  # noqa: BLE001 - 渲染失败不影响主流程
        logger.warning("讲解视频渲染异常", exc_info=True)
        notices.append("⚠️ 渲染失败：内部错误。")
        return produced
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
