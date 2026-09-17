"""作业照片的"入库即增强 + 分块放大"。

为什么需要：真实作业照片里，印刷小字（英语连线题、加点的字、竖式计算的小数字）
在 1600px 的整页图里只有几个像素高，模型为了看清会自己反复裁图放大——
实测一次 7 页批改裁了 100 多个小区域、看了 85 张图，占了整轮 17 分钟的大部分，
而且这些图会留在会话历史里，把请求体顶到 413。

所以入库时就把活干好：
  - 增强版整页（自动对比度 + 轻锐化）：作为**喂给模型的视觉输入**，比原图清楚；
  - 分块图（默认 3 行 × 2 列、8% 重叠、放大 2 倍）：放在 `.derived/tiles/`，
    分行分行地覆盖整页，模型看不清时**直接看这些文件**，不必自己裁。

产物都在 `.derived/` 下（点开头 → 不会被登记成产出物，也不占配额统计）。
任何失败都只记日志：这是"提升可读性"的增强，不能影响材料入库。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_TILE_DIM = 2400  # 单块放大后的上限，太大反而浪费 payload
# 喂给模型的整页图上限：和 pipeline.images 的 _MAX_IMAGE_DIM 保持一致
_MODEL_PAGE_DIM = 1600


def _derived_dir(workspace_root: Path) -> Path:
    path = workspace_root / ".derived"
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_path(workspace_root: Path, stem: str) -> Path:
    """分块图清单（记录切了几行几列、都有哪些文件），供提示词按需列出。"""
    return _derived_dir(workspace_root) / "tiles" / f"{stem}.tiles.json"


def load_tiles(workspace_root: Path, stem: str) -> list[str]:
    """读清单里的分块图绝对路径；没有就返回空列表。"""
    path = manifest_path(workspace_root, stem)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(p) for p in (data.get("files") or []) if Path(str(p)).is_file()]


def enhance_sync(src: Path, workspace_root: Path) -> Path | None:
    """生成增强版整页图（自动对比度 + 轻锐化），并压到模型友好尺寸。

    这张图是**喂给模型的视觉输入**：比手机原图清楚，但尺寸控制在 1600px，
    避免把请求体撑大。高清细节留给分块图（它们是从原图切的）。
    """
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    try:
        with Image.open(src) as img:
            page = img.convert("RGB")
        page = ImageOps.autocontrast(page, cutoff=1)
        page = ImageEnhance.Contrast(page).enhance(1.15)
        page = page.filter(ImageFilter.UnsharpMask(radius=2, percent=120, threshold=3))
        if max(page.size) > _MODEL_PAGE_DIM:
            scale = _MODEL_PAGE_DIM / max(page.size)
            page = page.resize(
                (max(1, round(page.width * scale)), max(1, round(page.height * scale))),
                Image.LANCZOS,
            )
        target = _derived_dir(workspace_root) / f"{src.stem}.enh.jpg"
        page.save(target, "JPEG", quality=88, optimize=True)
        return target
    except Exception:  # noqa: BLE001
        logger.warning("enhance failed: %s", src, exc_info=True)
        return None


def tiles_sync(
    src: Path,
    workspace_root: Path,
    *,
    rows: int = 4,
    cols: int = 3,
    overlap: float = 0.08,
    scale: float = 2.0,
) -> list[Path]:
    """把**原图**切成 rows×cols 块并放大，返回按"从上到下、从左到右"排序的文件列表。

    注意：这里必须从原始分辨率切（不能切压缩后的整页图），否则小字细节就丢了 ——
    之前模型靠自己裁图放大才能看清，就是因为它拿到的是压缩过的整页。
    """
    from PIL import Image

    rows = max(1, rows)
    cols = max(1, cols)
    made: list[Path] = []
    try:
        with Image.open(src) as img:
            page = img.convert("RGB")
            width, height = page.size
            tile_w = width / cols
            tile_h = height / rows
            pad_x = tile_w * overlap
            pad_y = tile_h * overlap
            out_dir = _derived_dir(workspace_root) / "tiles"
            out_dir.mkdir(parents=True, exist_ok=True)
            for row in range(rows):
                for col in range(cols):
                    left = max(0, int(col * tile_w - pad_x))
                    upper = max(0, int(row * tile_h - pad_y))
                    right = min(width, int((col + 1) * tile_w + pad_x))
                    lower = min(height, int((row + 1) * tile_h + pad_y))
                    tile = page.crop((left, upper, right, lower))
                    factor = min(scale, _MAX_TILE_DIM / max(tile.size))
                    if factor > 1.0:
                        tile = tile.resize(
                            (round(tile.width * factor), round(tile.height * factor)),
                            Image.LANCZOS,
                        )
                    target = out_dir / f"{src.stem}_r{row + 1}c{col + 1}.jpg"
                    tile.save(target, "JPEG", quality=88, optimize=True)
                    made.append(target)
            manifest_path(workspace_root, src.stem).write_text(
                json.dumps(
                    {
                        "source": str(src),
                        "rows": rows,
                        "cols": cols,
                        "files": [str(p) for p in made],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
    except Exception:  # noqa: BLE001
        logger.warning("tiling failed: %s", src, exc_info=True)
    return made
