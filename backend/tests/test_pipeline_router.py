from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest
from sqlmodel import Session

from app import events
from app.models import Material, MaterialKind, MaterialStatus, Thread, User
from app.pipeline import router as pipeline_router


def _seed(engine, settings, kind: MaterialKind, filename: str) -> tuple[str, str]:
    ws = settings.users_dir / "u1" / "threads" / "t1" / "workspace"
    ws.mkdir(parents=True)
    stored = ws / f"stored_{filename}"
    stored.write_bytes(b"payload")
    with Session(engine) as session:
        session.add(User(id="u1", username="alice", password_hash="x"))
        session.commit()
        session.add(Thread(id="t1", user_id="u1"))
        session.commit()
        material = Material(
            id="m1",
            thread_id="t1",
            filename=filename,
            stored_path=str(stored),
            kind=kind,
        )
        session.add(material)
        session.commit()
    return "m1", str(stored)


async def test_process_image_marks_ready(engine, settings):
    material_id, _ = _seed(engine, settings, MaterialKind.image, "photo.jpg")
    await pipeline_router.process_material(material_id, settings)
    with Session(engine) as session:
        material = session.get(Material, material_id)
        assert material.status == MaterialStatus.ready
        assert material.error is None
        assert "photo.jpg" in material.summary


async def test_process_document_uses_markitdown(engine, settings, monkeypatch):
    material_id, _ = _seed(engine, settings, MaterialKind.document, "essay.docx")

    class FakeResult:
        text_content = "这是文档的正文内容"

    class FakeMarkItDown:
        def convert(self, path: str) -> FakeResult:
            return FakeResult()

    fake_module = ModuleType("markitdown")
    fake_module.MarkItDown = FakeMarkItDown
    monkeypatch.setitem(sys.modules, "markitdown", fake_module)

    await pipeline_router.process_material(material_id, settings)
    with Session(engine) as session:
        material = session.get(Material, material_id)
        assert material.status == MaterialStatus.ready
        assert material.summary == "这是文档的正文内容"


async def test_process_audio_transcribes(engine, settings, monkeypatch, tmp_path):
    material_id, _ = _seed(engine, settings, MaterialKind.audio, "reading.mp3")

    async def fake_to_f32(src, dst, settings):
        Path(dst).write_bytes(b"\x00" * 32)
        return Path(dst)

    async def fake_transcribe(self, pcm_path):
        assert Path(pcm_path).read_bytes() == b"\x00" * 32
        return "zh", "学生朗读的全文转写"

    monkeypatch.setattr(pipeline_router.media, "to_f32_pcm", fake_to_f32)
    monkeypatch.setattr(pipeline_router.AsrClient, "transcribe_f32", fake_transcribe)

    queue = events.subscribe("t1")
    await pipeline_router.process_material(material_id, settings)

    with Session(engine) as session:
        material = session.get(Material, material_id)
        assert material.status == MaterialStatus.ready
        assert material.transcript == "学生朗读的全文转写"
        assert material.summary == "学生朗读的全文转写"
    event = queue.get_nowait()
    assert "event: material_status" in event
    assert '"status": "ready"' in event


async def test_process_video_extracts_frames_and_transcribes(
    engine, settings, monkeypatch
):
    material_id, _ = _seed(engine, settings, MaterialKind.video, "clip.mp4")

    async def fake_to_f32(src, dst, settings):
        Path(dst).write_bytes(b"\x00" * 16)
        return Path(dst)

    async def fake_extract(video, out_dir, prefix, settings):
        frames = []
        for i in range(3):
            frame = out_dir / f"{prefix}_{i}.jpg"
            frame.write_bytes(b"jpg")
            frames.append(frame)
        return frames

    async def fake_transcribe(self, pcm_path):
        return "zh", "视频讲解转写"

    monkeypatch.setattr(pipeline_router.media, "to_f32_pcm", fake_to_f32)
    monkeypatch.setattr(pipeline_router.media, "extract_keyframes", fake_extract)
    monkeypatch.setattr(pipeline_router.AsrClient, "transcribe_f32", fake_transcribe)

    async def fake_has_video_stream(src, settings):
        return True

    monkeypatch.setattr(
        pipeline_router.media, "has_video_stream", fake_has_video_stream
    )

    await pipeline_router.process_material(material_id, settings)

    with Session(engine) as session:
        material = session.get(Material, material_id)
        assert material.status == MaterialStatus.ready
        assert material.transcript == "视频讲解转写"
    frames = sorted(
        (settings.users_dir / "u1" / "threads" / "t1" / "workspace" / "keyframes").glob(
            "m1_*.jpg"
        )
    )
    assert len(frames) == 3


async def test_process_video_without_video_stream_falls_back_to_transcribe(
    engine, settings, monkeypatch
):
    """MediaRecorder 录音也是 .webm 容器：无视频流时跳过抽帧、只转写。"""
    material_id, _ = _seed(engine, settings, MaterialKind.video, "录音.webm")

    async def fake_to_f32(src, dst, settings):
        Path(dst).write_bytes(b"\x00" * 16)
        return Path(dst)

    async def fake_no_video(src, settings):
        return False

    async def extract_must_not_run(video, out_dir, prefix, settings):
        raise AssertionError("extract_keyframes must not run for audio-only files")

    async def fake_transcribe(self, pcm_path):
        return "zh", "纯音频转写"

    monkeypatch.setattr(pipeline_router.media, "to_f32_pcm", fake_to_f32)
    monkeypatch.setattr(pipeline_router.media, "has_video_stream", fake_no_video)
    monkeypatch.setattr(
        pipeline_router.media, "extract_keyframes", extract_must_not_run
    )
    monkeypatch.setattr(pipeline_router.AsrClient, "transcribe_f32", fake_transcribe)

    await pipeline_router.process_material(material_id, settings)

    with Session(engine) as session:
        material = session.get(Material, material_id)
        assert material.status == MaterialStatus.ready
        assert material.transcript == "纯音频转写"


async def test_process_failure_marks_failed_and_publishes_event(
    engine, settings, monkeypatch
):
    material_id, _ = _seed(engine, settings, MaterialKind.audio, "broken.mp3")

    async def boom(src, dst, settings):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(pipeline_router.media, "to_f32_pcm", boom)

    queue = events.subscribe("t1")
    await pipeline_router.process_material(material_id, settings)

    with Session(engine) as session:
        material = session.get(Material, material_id)
        assert material.status == MaterialStatus.failed
        assert material.error == "ffmpeg exploded"
    event = queue.get_nowait()
    assert '"status": "failed"' in event
    assert "ffmpeg exploded" in event


async def test_process_image_downscales_large_photo(engine, settings):
    """手机原图（3000x4000）应被缩到 1600px 以内，避免模型 API 413。"""
    from PIL import Image

    material_id, _ = _seed(engine, settings, MaterialKind.image, "big.jpg")
    with Session(engine) as session:
        stored = Path(session.get(Material, material_id).stored_path)
    img = Image.new("RGB", (3000, 4000), "white")
    img.save(stored, "JPEG")

    await pipeline_router.process_material(material_id, settings)

    with Session(engine) as session:
        material = session.get(Material, material_id)
        assert material.status == MaterialStatus.ready
    with Image.open(stored) as result:
        assert max(result.size) <= 1600
