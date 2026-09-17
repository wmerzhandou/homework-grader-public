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


async def image_summary(path: Path, workspace_root: Path | None = None) -> str:
    """入库时做"高清分块 + 模型用增强副本"，并且**不再改动原图**。

    **顺序很重要（关系识别质量）**：
      1) 在**原始分辨率**上切分块图（小字细节只在这里，模型优先看它们）；
      2) 另存一份增强整页（≤1600px）**作为喂给模型的视觉输入** —— 请求体大小由它决定；
      3) 原图原样保留：用户下载、以后重新切块、模型需要时再放大核对，都靠它。

    以前是"入库就把原图原地压到 1600px"，等于先把细节毁掉：模型只能在自己手里
    这张糊图上裁 120×100 的小块再放 9 倍，一次批改裁了 100 多张 —— 慢，而且
    那些图全留在会话历史里，最后把请求体顶到 413。

    分块图放在 `.derived/tiles/`，模型优先看它们；**没有禁止它自己裁图**——
    真遇到更细的地方，仍然可以自己放大核对（识别效果优先）。
    """
    from . import tiles

    if workspace_root is None:
        await downscale_image(path)
        return f"图片文件 {path.name}，已作为图片输入提供给批改模型"

    made = await asyncio.to_thread(tiles.tiles_sync, path, workspace_root)
    enhanced = await asyncio.to_thread(tiles.enhance_sync, path, workspace_root)
    if made:
        return (
            f"图片文件 {path.name}，已提供增强版整页；另按原图切好 {len(made)} 张高清分块图，"
            f"小字优先看分块图（更省时间），**仍然看不清时可以从原图上继续裁切放大**"
        )
    if enhanced is not None:
        return f"图片文件 {path.name}，已提供增强版整页"
    return f"图片文件 {path.name}，已作为图片输入提供给批改模型"


def enhanced_for(path: Path, workspace_root: Path) -> Path:
    """喂给模型的图：优先用增强版，没有就用原文件。"""
    enhanced = workspace_root / ".derived" / f"{path.stem}.enh.jpg"
    return enhanced if enhanced.is_file() else path
