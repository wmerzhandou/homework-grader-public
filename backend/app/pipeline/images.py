"""Material kind detection and image handling."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..models import MaterialKind

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".amr"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

# 手机原图太大，base64 后会超出模型 API 的请求体上限（DeepSeek 413）。
# 1600px 足够看清作业字迹。
_MAX_IMAGE_DIM = 1600
_JPEG_QUALITY = 85


def detect_kind(filename: str) -> MaterialKind:
    ext = Path(filename).suffix.lower()
    if ext in _IMAGE_EXTS:
        return MaterialKind.image
    if ext in _AUDIO_EXTS:
        return MaterialKind.audio
    if ext in _VIDEO_EXTS:
        return MaterialKind.video
    return MaterialKind.document


def _downscale_sync(path: Path) -> None:
    """Resize in place if either dimension exceeds _MAX_IMAGE_DIM."""
    from PIL import Image

    try:
        with Image.open(path) as img:
            width, height = img.size
            if max(width, height) <= _MAX_IMAGE_DIM:
                return
            scale = _MAX_IMAGE_DIM / max(width, height)
            new_size = (round(width * scale), round(height * scale))
            resized = img.resize(new_size, Image.LANCZOS)
            if path.suffix.lower() in {".jpg", ".jpeg"}:
                resized.convert("RGB").save(path, "JPEG", quality=_JPEG_QUALITY)
            else:
                resized.save(path)
    except Exception:
        # 打不开/缩放失败不阻断材料：原样交给 codex 的视觉输入处理
        logger.warning("could not downscale image %s", path, exc_info=True)
        return
    logger.info("downscaled image %s from %sx%s to %sx%s", path.name, width, height, *new_size)


async def downscale_image(path: Path) -> None:
    await asyncio.to_thread(_downscale_sync, path)


async def image_summary(path: Path) -> str:
    """Downscale for the model payload; the same file is passed to codex."""
    await downscale_image(path)
    return f"图片文件 {path.name}，已作为图片输入提供给批改模型"
