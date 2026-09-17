"""Async preprocessing pipeline dispatcher for uploaded materials.

Failures set status=failed with an error message and publish an SSE
material_status event; they never propagate to the upload endpoint.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlmodel import Session

from .. import events
from ..codex_service import workspace
from ..config import Settings, get_settings
from ..db import get_engine
from ..models import Material, MaterialKind, MaterialStatus, Thread
from . import docs, images, media
from .asr_client import AsrClient

logger = logging.getLogger(__name__)


async def _transcribe(path: Path, settings: Settings) -> tuple[str, str]:
    pcm_path = path.with_suffix(path.suffix + ".f32")
    try:
        await media.to_f32_pcm(path, pcm_path, settings)
        async with AsrClient(settings) as asr:
            return await asr.transcribe_f32(pcm_path)
    finally:
        pcm_path.unlink(missing_ok=True)


async def process_material(material_id: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    with Session(get_engine()) as session:
        material = session.get(Material, material_id)
        if material is None:
            logger.warning("material %s vanished before processing", material_id)
            return
        thread = session.get(Thread, material.thread_id)
        if thread is None:
            return
        user_id = thread.user_id
        thread_id = material.thread_id
        stored_path = Path(material.stored_path)

    try:
        summary, transcript, text_path = await _process(material, stored_path, user_id, settings)
        status, error = MaterialStatus.ready, None
    except Exception as exc:  # noqa: BLE001 - pipeline failures must not escape
        logger.exception("material %s preprocessing failed", material_id)
        summary, transcript, text_path = "", "", None
        status, error = MaterialStatus.failed, str(exc)[:500]

    with Session(get_engine()) as session:
        material = session.get(Material, material_id)
        if material is None:
            return
        material.status = status
        material.summary = summary
        material.transcript = transcript
        material.text_path = text_path
        material.error = error
        session.add(material)
        session.commit()

    payload: dict = {"material_id": material_id, "status": status.value}
    if error:
        payload["error"] = error
    events.publish(thread_id, "material_status", payload)


def derived_dir(settings: Settings, user_id: str, thread_id: str) -> Path:
    """会话工作区里的"可读文本"目录（点开头 → 不会被登记成产出物）。"""
    path = workspace.workspace_dir(settings, user_id, thread_id) / ".derived"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_derived(settings: Settings, user_id: str, thread_id: str, material_id: str, suffix: str, text: str) -> str:
    """把预处理得到的文本落盘，返回绝对路径（供模型按需读取）。"""
    target = derived_dir(settings, user_id, thread_id) / f"{material_id}{suffix}"
    target.write_text(text, encoding="utf-8")
    return str(target)


async def _process(
    material: Material, stored_path: Path, user_id: str, settings: Settings
) -> tuple[str, str, str | None]:
    """Returns (summary, transcript, 可读文本路径)。"""
    if material.kind == MaterialKind.image:
        return await images.image_summary(stored_path), "", None
    if material.kind == MaterialKind.document:
        full = await docs.document_text(stored_path)
        path = _write_derived(settings, user_id, material.thread_id, material.id, ".md", full)
        return full[:500], "", path
    if material.kind == MaterialKind.audio:
        _language, text = await _transcribe(stored_path, settings)
        path = _write_derived(settings, user_id, material.thread_id, material.id, ".txt", text)
        return text[:500], text, path
    if material.kind == MaterialKind.video:
        if not await media.has_video_stream(stored_path, settings):
            # MediaRecorder 录音也是 .webm：纯音频文件按音频处理
            material.kind = MaterialKind.audio
            with Session(get_engine()) as session:
                merged = session.get(Material, material.id)
                if merged is not None:
                    merged.kind = MaterialKind.audio
                    session.add(merged)
                    session.commit()
        else:
            try:
                frames_dir = workspace.keyframes_dir(settings, user_id, material.thread_id)
                await media.extract_keyframes(stored_path, frames_dir, material.id, settings)
            except Exception:
                logger.warning(
                    "keyframe extraction failed for %s, continuing audio-only",
                    stored_path,
                    exc_info=True,
                )
        _language, text = await _transcribe(stored_path, settings)
        path = _write_derived(settings, user_id, material.thread_id, material.id, ".txt", text)
        return text[:500], text, path
    raise RuntimeError(f"unsupported material kind: {material.kind}")
