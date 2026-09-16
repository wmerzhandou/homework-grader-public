"""Material upload and file download endpoints."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import get_current_user, user_from_token
from ..codex_service import workspace
from ..config import get_settings
from ..db import get_session
from ..models import Artifact, Material, Thread, User
from ..pipeline import images, router as pipeline
from ..security import ensure_app_secret, verify_media_ticket

router = APIRouter(tags=["materials"])

_MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class MaterialOut(BaseModel):
    id: str
    filename: str
    kind: str
    status: str
    purpose: str = ""
    summary: str = ""
    transcript: str = ""
    error: str | None = None


class UploadResponse(BaseModel):
    materials: list[MaterialOut]


def _thread_or_404(session: Session, thread_id: str, user: User) -> Thread:
    thread = session.get(Thread, thread_id)
    if thread is None or thread.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thread not found")
    return thread


@router.post("/api/threads/{thread_id}/materials", response_model=UploadResponse)
async def upload_materials(
    thread_id: str,
    files: list[UploadFile] | None = File(default=None),
    file: UploadFile | None = File(default=None),
    purposes: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> UploadResponse:
    thread = _thread_or_404(session, thread_id, user)
    settings = get_settings()

    uploads: list[UploadFile] = list(files or [])
    if file is not None:
        uploads.append(file)
    if not uploads:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="at least one file is required (field 'files' or 'file')",
        )
    if len(uploads) > settings.max_upload_files:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"单次最多上传 {settings.max_upload_files} 个文件",
        )

    # 每用户磁盘配额：上传前先算该用户已用量（**含模型产出的文件**），避免刷爆磁盘
    user_thread_ids = select(Thread.id).where(Thread.user_id == user.id)
    used = sum(
        _material_size(m)
        for m in session.exec(select(Material).where(Material.thread_id.in_(user_thread_ids))).all()
    ) + sum(
        _artifact_size(a)
        for a in session.exec(select(Artifact).where(Artifact.thread_id.in_(user_thread_ids))).all()
    )

    purpose_list: list[str] = []
    if purposes:
        try:
            parsed = json.loads(purposes)
            if not isinstance(parsed, list):
                raise ValueError
            purpose_list = [str(p) for p in parsed]
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="purposes must be a JSON array",
            ) from None
    if purpose_list and len(purpose_list) != len(uploads):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="purposes length must match uploads length",
        )

    ws = workspace.workspace_dir(settings, user.id, thread.id)
    created: list[Material] = []
    total_bytes = 0
    for index, upload in enumerate(uploads):
        filename = upload.filename or f"file-{index}"
        kind = images.detect_kind(filename)
        material = Material(
            thread_id=thread.id,
            filename=filename,
            stored_path="",
            kind=kind,
            purpose=purpose_list[index] if purpose_list else "",
        )
        session.add(material)
        session.commit()

        safe_name = filename.replace("/", "_").replace("\\", "_")
        stored = ws / f"{material.id}_{safe_name}"
        size = 0
        with stored.open("wb") as fh:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                total_bytes += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    stored.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"{filename} exceeds upload size limit",
                    )
                if total_bytes > settings.max_upload_total_bytes:
                    stored.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="本次上传总大小超出限制",
                    )
                if used + total_bytes > settings.user_storage_quota_bytes:
                    stored.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="账号存储空间已满，请先删除不再需要的会话",
                    )
                fh.write(chunk)
        material.stored_path = str(stored)
        session.add(material)
        session.commit()
        created.append(material)

    for material in created:
        asyncio.create_task(pipeline.process_material(material.id))

    return UploadResponse(
        materials=[
            MaterialOut(
                id=m.id,
                filename=m.filename,
                kind=m.kind.value,
                status=m.status.value,
                purpose=m.purpose,
            )
            for m in created
        ]
    )


@router.get("/api/materials/{material_id}/file")
def download_material(
    material_id: str,
    ticket: str | None = None,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> FileResponse:
    # 浏览器 <img>/<audio>/<video> 标签无法携带 Authorization 头，
    # 因此除 Bearer 头外接受短时票据 ?ticket=（15 分钟、绑定用户、仅对下载接口有效）。
    # 不再接受长期登录 token 出现在 URL 里。
    user: User | None = None
    if authorization and authorization.startswith("Bearer "):
        user = user_from_token(session, authorization.removeprefix("Bearer ").strip())
    elif ticket:
        settings = get_settings()
        secret = ensure_app_secret(settings.data_dir, settings.app_secret)
        user_id = verify_media_ticket(secret, ticket)
        if user_id:
            user = session.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
        )
    material = session.get(Material, material_id)
    if material is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="material not found")
    _thread_or_404(session, material.thread_id, user)
    return FileResponse(material.stored_path, filename=material.filename)


def _material_size(material: Material) -> int:
    if not material.stored_path:
        return 0
    try:
        path = Path(material.stored_path)
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _artifact_size(artifact: Artifact) -> int:
    """产出物也算进配额：模型生成的视频/图片一样占盘。预览副本一并计入。"""
    total = 0
    for raw in (artifact.stored_path, artifact.preview_path):
        if not raw:
            continue
        try:
            path = Path(raw)
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total
