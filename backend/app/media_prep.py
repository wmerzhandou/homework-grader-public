"""产出物的"浏览器友好"预览副本：图片缩放、视频转码。

为什么需要：
- 模型生成的图片常常是几 MB 到几十 MB 的原图，直接塞进聊天窗口会卡；
- 视频如果编码是 H.265/AV1 或容器不被支持，iOS Safari 根本播不了 —— 转成 H.264/AAC 的 mp4 并加 faststart。

策略：
- 后台任务执行（不阻塞 turn 结束），完成后通过 SSE `artifact_ready` 通知前端；
- **原文件永远保留**，预览副本另存为 `<stem>.web.jpg` / `<stem>.web.mp4`，下载按钮仍给原文件；
- 任何失败都只记日志：预览是增强，不能影响主流程。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlmodel import Session

from . import events
from .config import get_settings
from .db import get_engine
from .models import Artifact

logger = logging.getLogger(__name__)

_TRANSCODE_TIMEOUT_S = 900.0
_IMAGE_PREVIEW_MAX_BYTES = 1 * 1024 * 1024  # 小于 1MB 的图不动它


async def _run(cmd: list[str]) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _out, err = await proc.communicate()
    return proc.returncode or 0, err or b""


async def _probe_video(path: Path) -> tuple[str, str] | None:
    """返回 (视频编码, 音频编码)；探测失败返回 None。"""
    settings = get_settings()
    ffprobe = settings.ffmpeg_bin.replace("ffmpeg", "ffprobe")
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
        "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return _parse_probe_csv(out.decode(errors="replace"))


_STREAM_KINDS = {"video", "audio", "subtitle", "data", "attachment"}


def _parse_probe_csv(text: str) -> tuple[str, str]:
    """解析 ffprobe 的 csv 输出。

    注意：`-show_entries stream=codec_type,codec_name` 的输出顺序由 ffprobe 决定，
    实测会输出 `h264,video`（编码在前）——所以不能假设字段顺序，按"哪个是流类型"来判定。
    """
    video_codec = audio_codec = ""
    for line in text.splitlines():
        parts = [p.strip() for p in line.strip().split(",") if p.strip()]
        kind = next((p for p in parts if p in _STREAM_KINDS), None)
        if kind is None:
            continue
        codec = next((p for p in parts if p not in _STREAM_KINDS), "")
        if kind == "video" and not video_codec:
            video_codec = codec
        elif kind == "audio" and not audio_codec:
            audio_codec = codec
    return (video_codec, audio_codec)


def _image_preview_sync(src: Path, max_dim: int) -> Path | None:
    from PIL import Image

    with Image.open(src) as img:
        width, height = img.size
        if max(width, height) <= max_dim and src.stat().st_size <= 8 * 1024 * 1024:
            return None
        scale = min(1.0, max_dim / max(width, height))
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        target = src.with_name(f"{src.stem}.web.jpg")
        img.convert("RGB").resize(size, Image.LANCZOS).save(target, "JPEG", quality=85)
        return target


async def prepare_artifact(artifact_id: str) -> None:
    """为图片/视频生成预览副本并落库；失败只记日志。"""
    settings = get_settings()
    with Session(get_engine()) as session:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None or artifact.preview_path:
            return
        kind = artifact.kind
        source = Path(artifact.stored_path)
        thread_id = artifact.thread_id
    if not source.is_file():
        return

    preview: Path | None = None
    try:
        if kind == "image":
            preview = await asyncio.to_thread(
                _image_preview_sync, source, settings.artifact_image_max_dim
            )
            if preview is None:
                # 已经够小，不需要副本：把原文件当作预览，前端就不会显示"准备中"
                preview = source
        elif kind == "video":
            probe = await _probe_video(source)
            video_codec, audio_codec = probe if probe else ("", "")
            needs = not (
                video_codec == "h264"
                and source.suffix.lower() in {".mp4", ".m4v"}
                and (audio_codec in {"aac", ""})
            )
            if not needs:
                # 已经是浏览器友好的 h264/mp4：不需要转码，直接用原文件预览
                preview = source
            else:
                target = source.with_name(f"{source.stem}.web.mp4")
                code, err = await asyncio.wait_for(
                    _run(
                        [
                            settings.ffmpeg_bin, "-y", "-i", str(source),
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                            "-vf", "scale='min(1280,iw)':-2",
                            "-c:a", "aac", "-b:a", "128k",
                            "-movflags", "+faststart",
                            str(target),
                        ]
                    ),
                    timeout=_TRANSCODE_TIMEOUT_S,
                )
                if code != 0:
                    logger.warning("视频转码失败 %s: %s", source, err[-300:])
                elif target.is_file():
                    preview = target
    except asyncio.TimeoutError:
        logger.warning("视频转码超时：%s", source)
    except Exception:  # noqa: BLE001 - 预览失败不影响主流程
        logger.warning("生成预览副本失败：%s", source, exc_info=True)

    with Session(get_engine()) as session:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None:
            return
        if preview is not None:
            artifact.preview_path = str(preview)
            session.add(artifact)
            session.commit()
        payload = {
            "artifact_id": artifact_id,
            "thread_id": thread_id,
            "has_preview": preview is not None,
        }
    events.publish(thread_id, "artifact_ready", payload)
