from __future__ import annotations

from pathlib import Path

import pytest

from app.codex_service import context
from app.codex_service.workspace import keyframes_dir
from app.models import Material, MaterialKind, MaterialStatus


def _material(**kwargs) -> Material:
    defaults = {
        "id": "m1",
        "thread_id": "t1",
        "filename": "photo.jpg",
        "stored_path": "/tmp/ws/photo.jpg",
        "kind": MaterialKind.image,
        "status": MaterialStatus.ready,
    }
    return Material(**(defaults | kwargs))


def test_materials_block_empty(settings):
    assert context.build_materials_block(settings, []) == ""


def test_materials_block_with_purpose_summary_transcript(settings):
    materials = [
        _material(purpose="数学口算作业", summary="一张口算题照片"),
        _material(
            id="m2",
            filename="reading.mp3",
            kind=MaterialKind.audio,
            transcript="学生的完整朗读录音转写文本",
            summary="朗读录音",
        ),
    ]
    block = context.build_materials_block(settings, materials)
    assert "photo.jpg" in block
    assert "数学口算作业" in block
    assert "一张口算题照片" in block
    assert "完整转写文本" in block


def test_materials_block_summary_truncated(settings):
    materials = [_material(summary="x" * 1000)]
    block = context.build_materials_block(settings, materials)
    assert "x" * 501 not in block
    assert "x" * 500 in block


def test_materials_block_failed_material(settings):
    materials = [_material(status=MaterialStatus.failed, error="ffmpeg boom", summary="")]
    block = context.build_materials_block(settings, materials)
    assert "预处理失败" in block
    assert "ffmpeg boom" in block


def test_build_turn_input_text_only(settings):
    items = context.build_turn_input(settings, "u1", "t1", "帮我批改", [])
    assert items == [{"type": "text", "text": "帮我批改"}]


def test_build_turn_input_with_images_and_keyframes(settings):
    ws = settings.users_dir / "u1" / "threads" / "t1" / "workspace"
    ws.mkdir(parents=True)
    image = ws / "photo.jpg"
    image.write_bytes(b"fake")
    frames = keyframes_dir(settings, "u1", "t1")
    (frames / "v1_0.jpg").write_bytes(b"f0")
    (frames / "v1_1.jpg").write_bytes(b"f1")
    (frames / "v1_2.jpg").write_bytes(b"f2")

    materials = [
        _material(stored_path=str(image), summary="照片"),
        _material(
            id="v1",
            filename="clip.mp4",
            kind=MaterialKind.video,
            stored_path=str(ws / "clip.mp4"),
            transcript="讲解视频",
        ),
    ]
    items = context.build_turn_input(settings, "u1", "t1", "批改一下", materials)

    assert items[0]["type"] == "text"
    assert "批改一下" in items[0]["text"]
    assert "材料清单" in items[0]["text"]
    image_items = [i for i in items if i["type"] == "localImage"]
    assert image_items == [
        {"type": "localImage", "path": str(image)},
        {"type": "localImage", "path": str(frames / "v1_0.jpg")},
        {"type": "localImage", "path": str(frames / "v1_1.jpg")},
        {"type": "localImage", "path": str(frames / "v1_2.jpg")},
    ]


def test_image_inputs_skip_not_ready_materials(settings):
    materials = [_material(status=MaterialStatus.processing)]
    assert context.image_inputs_for_materials(settings, "u1", "t1", materials) == []


def test_grading_instructions_mention_result_file():
    assert "grading_result.json" in context.GRADING_INSTRUCTIONS
    assert "knowledge_points" in context.GRADING_INSTRUCTIONS
