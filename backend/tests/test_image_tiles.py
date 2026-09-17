"""入库增强 + 分块：分块来自原图、原图不被压小、提示词里带分块路径。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from app.pipeline import images, tiles


def _make_photo(path: Path, size: tuple[int, int] = (3000, 4000)) -> None:
    Image.new("RGB", size, (245, 245, 240)).save(path, "JPEG", quality=90)


def test_tiles_are_cut_from_full_resolution_and_manifest_written(tmp_path):
    photo = tmp_path / "page.jpg"
    _make_photo(photo)

    made = tiles.tiles_sync(photo, tmp_path, rows=4, cols=3)

    assert len(made) == 12
    biggest = max(Image.open(p).size for p in made)
    # 从 3000×4000 原图切：每块本身就有 1000px 级别，放大后远大于 1600 压缩版的碎块
    assert biggest[0] > 1200 and biggest[1] > 1200
    listed = tiles.load_tiles(tmp_path, photo.stem)
    assert [Path(p).name for p in listed] == [p.name for p in made]
    manifest = json.loads(tiles.manifest_path(tmp_path, photo.stem).read_text(encoding="utf-8"))
    assert manifest["rows"] == 4 and manifest["cols"] == 3


async def test_image_summary_keeps_original_and_caps_model_copy(tmp_path):
    """关键回归：原图保持原始分辨率（细节留给分块），模型副本压到 1600px 以内。"""
    photo = tmp_path / "page.jpg"
    _make_photo(photo)

    summary = await images.image_summary(photo, tmp_path)

    assert "分块" in summary
    assert Image.open(photo).size == (3000, 4000), "原图不能被原地压小"
    model_copy = tmp_path / ".derived" / f"{photo.stem}.enh.jpg"
    assert model_copy.is_file()
    assert max(Image.open(model_copy).size) <= 1600, "喂给模型的副本要控制体积"
    assert len(tiles.load_tiles(tmp_path, photo.stem)) == 12


async def test_image_summary_without_workspace_still_downscales(tmp_path):
    """老路径（没有工作区）仍然按原逻辑压小，避免回归。"""
    photo = tmp_path / "page.jpg"
    _make_photo(photo)

    await images.image_summary(photo)

    assert max(Image.open(photo).size) <= 1600


def test_material_entry_lists_tile_paths(tmp_path, monkeypatch):
    from app.codex_service import context
    from app.config import get_settings

    photo = tmp_path / "abc_123.jpg"
    _make_photo(photo)
    tiles.tiles_sync(photo, tmp_path, rows=4, cols=3)

    class _Material:
        filename = "abc_123.jpg"
        kind = __import__("app.models", fromlist=["MaterialKind"]).MaterialKind.image
        stored_path = str(photo)
        status = "ready"
        purpose = None
        summary = "图片文件"
        transcript = None
        error = None

    entry = context._material_entry(get_settings(), _Material())

    assert "高清分块图" in entry
    assert "_r1c1.jpg" in entry
    assert "识别准确优先" in entry, "不能禁止模型自己裁图放大"
