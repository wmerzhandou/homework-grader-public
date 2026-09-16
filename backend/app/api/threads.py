"""Thread CRUD, message history, and turn execution endpoints."""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi import Header
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import get_current_user, user_from_token
from ..codes import extract_references
from ..codex_service import workspace
from ..codex_service.manager import get_manager
from ..config import get_settings
from ..db import get_session
from ..models import DEFAULT_TASK_TYPE, Artifact, Material, Message, Thread, User, iso_utc
from ..security import ensure_app_secret, verify_media_ticket
from ..thread_codes import ensure_code

router = APIRouter(prefix="/api/threads", tags=["threads"])

TaskType = Literal["auto", "grading", "english_passage", "general"]


class ThreadCreateRequest(BaseModel):
    title: str | None = None
    task_type: TaskType | None = None


class ThreadOut(BaseModel):
    id: str
    title: str
    code: str = ""
    task_type: str
    created_at: str
    # 会话里有多少材料/消息（用于引用选择器与引用预览）
    material_count: int = 0
    message_count: int = 0


class MaterialOut(BaseModel):
    id: str
    filename: str
    kind: str
    status: str
    purpose: str = ""
    summary: str = ""
    transcript: str = ""
    error: str | None = None


class ArtifactOut(BaseModel):
    """模型产出的文件（对应 /api/threads/{thread_id}/artifacts/{id}/file）"""

    id: str
    filename: str
    kind: str
    size: int
    oversized: bool = False
    """是否已生成浏览器友好的预览副本（图片缩放 / 视频转 H.264）"""
    has_preview: bool = False
    created_at: str


class MessageOut(BaseModel):
    id: str
    turn_id: str | None
    role: str
    content: str
    process_log: str | None = None
    grading_result: dict | None = None
    result_type: str | None = None
    materials: list[MaterialOut] = []
    artifacts: list[ArtifactOut] = []
    references: list[dict] = []
    created_at: str


class ThreadDetailOut(ThreadOut):
    messages: list[MessageOut]
    materials: list[MaterialOut]


class TurnRequest(BaseModel):
    text: str
    material_ids: list[str] | None = None


class TurnResponse(BaseModel):
    turn_id: str


def _thread_or_404(session: Session, thread_id: str, user: User) -> Thread:
    thread = session.get(Thread, thread_id)
    if thread is None or thread.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thread not found")
    return thread


def _thread_out(thread: Thread) -> ThreadOut:
    return ThreadOut(
        id=thread.id,
        title=thread.title,
        code=thread.code,
        task_type=thread.task_type,
        created_at=iso_utc(thread.created_at),
    )


def _thread_counts(session: Session, thread_ids: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    """一次查出这些会话的材料数与消息数（避免 N+1）。"""
    material_counts: dict[str, int] = {}
    message_counts: dict[str, int] = {}
    if not thread_ids:
        return material_counts, message_counts
    from sqlalchemy import func

    for thread_id, count in session.exec(
        select(Material.thread_id, func.count())
        .where(Material.thread_id.in_(thread_ids))
        .group_by(Material.thread_id)
    ).all():
        material_counts[thread_id] = int(count)
    for thread_id, count in session.exec(
        select(Message.thread_id, func.count())
        .where(Message.thread_id.in_(thread_ids))
        .group_by(Message.thread_id)
    ).all():
        message_counts[thread_id] = int(count)
    return material_counts, message_counts


@router.post("", response_model=ThreadOut)
def create_thread(
    body: ThreadCreateRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ThreadOut:
    thread = Thread(
        user_id=user.id,
        title=body.title or "未命名作业",
        task_type=body.task_type or DEFAULT_TASK_TYPE,
    )
    ensure_code(session, thread)
    session.add(thread)
    session.commit()
    return _thread_out(thread)


class ThreadRenameRequest(BaseModel):
    title: str


@router.patch("/{thread_id}", response_model=ThreadOut)
def rename_thread(
    thread_id: str,
    body: ThreadRenameRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ThreadOut:
    thread = _thread_or_404(session, thread_id, user)
    title = body.title.strip()
    if not title:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="title must not be empty"
        )
    thread.title = title[:50]
    session.add(thread)
    session.commit()
    return _thread_out(thread)


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_thread(
    thread_id: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> None:
    """删除会话：连带删除消息、材料、产出物记录和磁盘上的工作区。错题本条目保留。"""
    thread = _thread_or_404(session, thread_id, user)
    # 顺序很重要：产出物/材料/消息都引用 thread.id，且有外键约束（PRAGMA foreign_keys=ON），
    # 漏删任何一类都会让 DELETE 直接 500 —— 曾经就踩过这个坑。
    for artifact in session.exec(select(Artifact).where(Artifact.thread_id == thread.id)).all():
        session.delete(artifact)
    for message in session.exec(select(Message).where(Message.thread_id == thread.id)).all():
        session.delete(message)
    for material in session.exec(select(Material).where(Material.thread_id == thread.id)).all():
        session.delete(material)
    session.delete(thread)
    session.commit()

    shutil.rmtree(
        workspace.thread_dir(get_settings(), user.id, thread_id),
        ignore_errors=True,
    )


@router.get("", response_model=list[ThreadOut])
def list_threads(
    user: User = Depends(get_current_user), session: Session = Depends(get_session)
) -> list[ThreadOut]:
    threads = session.exec(
        select(Thread).where(Thread.user_id == user.id).order_by(Thread.created_at.desc())
    ).all()
    _backfill_codes(session, threads)
    material_counts, message_counts = _thread_counts(session, [t.id for t in threads])
    out: list[ThreadOut] = []
    for thread in threads:
        item = _thread_out(thread)
        item.material_count = material_counts.get(thread.id, 0)
        item.message_count = message_counts.get(thread.id, 0)
        out.append(item)
    return out


class ReferencePreviewRequest(BaseModel):
    text: str


class ReferencePreviewEntry(BaseModel):
    code: str
    thread_id: str
    title: str
    material_count: int
    message_count: int


class ReferencePreviewOut(BaseModel):
    references: list[ReferencePreviewEntry]
    unresolved: list[str]


@router.post("/reference-preview", response_model=ReferencePreviewOut)
def reference_preview(
    body: ReferencePreviewRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ReferencePreviewOut:
    """发送前预览：这段文字里的 #码 会引用到哪些会话、各带多少材料与对话索引。"""
    codes = extract_references(body.text)
    if not codes:
        return ReferencePreviewOut(references=[], unresolved=[])
    threads = list(
        session.exec(
            select(Thread).where(Thread.user_id == user.id, Thread.code.in_(codes))
        ).all()
    )
    by_code = {thread.code: thread for thread in threads}
    material_counts, message_counts = _thread_counts(session, [t.id for t in threads])
    references: list[ReferencePreviewEntry] = []
    unresolved: list[str] = []
    for code in codes:
        thread = by_code.get(code)
        if thread is None:
            unresolved.append(code)
            continue
        references.append(
            ReferencePreviewEntry(
                code=code,
                thread_id=thread.id,
                title=thread.title,
                material_count=material_counts.get(thread.id, 0),
                message_count=message_counts.get(thread.id, 0),
            )
        )
    return ReferencePreviewOut(references=references, unresolved=unresolved)


def _backfill_codes(session: Session, threads: list[Thread]) -> None:
    """老会话没有会话码时按需补齐（拉列表、打开会话时都会补）。"""
    changed = False
    for thread in threads:
        if not thread.code:
            ensure_code(session, thread)
            session.add(thread)
            changed = True
    if changed:
        session.commit()


def resolve_reference_codes(
    session: Session, user: User, text: str, current_thread_id: str
) -> tuple[list[str], list[str]]:
    """把消息里的 #CODE 解析成"可引用的会话 id"。

    返回 (命中的会话 id 列表, 认不出的码列表)。只认自己的会话；
    引用自己当前会话没有意义，直接忽略（不算错误）。
    """
    reference_ids: list[str] = []
    unresolved: list[str] = []
    for code in extract_references(text):
        ref = session.exec(
            select(Thread).where(Thread.user_id == user.id, Thread.code == code)
        ).first()
        if ref is None:
            unresolved.append(code)
        elif ref.id != current_thread_id:
            reference_ids.append(ref.id)
    return reference_ids, unresolved


def _material_out(mat: Material) -> MaterialOut:
    return MaterialOut(
        id=mat.id,
        filename=mat.filename,
        kind=mat.kind.value,
        status=mat.status.value,
        purpose=mat.purpose,
        summary=mat.summary,
        transcript=mat.transcript,
        error=mat.error,
    )


def _artifact_out(artifact: Artifact) -> ArtifactOut:
    return ArtifactOut(
        id=artifact.id,
        filename=artifact.filename,
        kind=artifact.kind,
        size=artifact.size,
        oversized=artifact.oversized,
        has_preview=bool(artifact.preview_path),
        created_at=iso_utc(artifact.created_at),
    )


@router.get("/{thread_id}", response_model=ThreadDetailOut)
def get_thread(
    thread_id: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ThreadDetailOut:
    thread = _thread_or_404(session, thread_id, user)
    _backfill_codes(session, [thread])
    messages = session.exec(
        select(Message).where(Message.thread_id == thread.id).order_by(Message.created_at)
    ).all()
    materials = session.exec(
        select(Material).where(Material.thread_id == thread.id).order_by(Material.created_at)
    ).all()
    materials_by_id = {mat.id: mat for mat in materials}
    artifacts = session.exec(
        select(Artifact).where(Artifact.thread_id == thread.id).order_by(Artifact.created_at)
    ).all()
    artifacts_by_message: dict[str, list[ArtifactOut]] = {}
    for artifact in artifacts:
        if artifact.message_id:
            artifacts_by_message.setdefault(artifact.message_id, []).append(_artifact_out(artifact))
    return ThreadDetailOut(
        **_thread_out(thread).model_dump(),
        messages=[
            MessageOut(
                id=m.id,
                turn_id=m.turn_id,
                role=m.role,
                content=m.content,
                process_log=m.process_log,
                grading_result=(
                    json.loads(m.grading_result_json) if m.grading_result_json else None
                ),
                result_type=m.result_type,
                materials=[
                    _material_out(materials_by_id[mid])
                    for mid in json.loads(m.material_ids_json or "[]")
                    if mid in materials_by_id
                ],
                artifacts=artifacts_by_message.get(m.id, []),
                references=json.loads(m.references_json) if m.references_json else [],
                created_at=iso_utc(m.created_at),
            )
            for m in messages
        ],
        materials=[_material_out(mat) for mat in materials],
    )


@router.post("/{thread_id}/turns", response_model=TurnResponse)
async def start_turn(
    thread_id: str,
    body: TurnRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> TurnResponse:
    thread = _thread_or_404(session, thread_id, user)
    if body.material_ids:
        known = {
            m.id
            for m in session.exec(
                select(Material).where(Material.thread_id == thread.id)
            ).all()
        }
        unknown = [mid for mid in body.material_ids if mid not in known]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown material ids: {unknown}",
            )
    turn_id = uuid.uuid4().hex
    text = body.text.strip()

    # 解析跨会话引用（#K7M2）：只认自己的会话；认不出的码原样回给用户提示
    reference_ids, unresolved_codes = resolve_reference_codes(
        session, user, body.text, thread.id
    )

    if not text and body.material_ids:
        # 用户只发了材料没写指令：给 codex 一个默认任务，历史里也不存空气泡
        text = "请阅读并分析这些材料。如果是作业，请批改；否则总结材料内容。"

    # 首个用户消息自动生成会话标题：优先用用户原文，纯材料提交用首个文件名
    if thread.title == "未命名作业":
        if body.text.strip():
            thread.title = body.text.strip()[:20]
        elif body.material_ids:
            first = session.get(Material, body.material_ids[0])
            if first is not None:
                thread.title = first.filename[:20]
        session.add(thread)
        session.commit()

    # 用户消息不在此处落库：由 manager 在拿到会话锁后写入，
    # 保证历史顺序严格是 user1 → assistant1 → user2 → assistant2。

    asyncio.create_task(
        get_manager().run_turn(
            user.id,
            thread.id,
            turn_id,
            text,
            body.material_ids,
            reference_ids=reference_ids,
            unresolved_codes=unresolved_codes,
        )
    )
    return TurnResponse(turn_id=turn_id)


@router.get("/{thread_id}/artifacts/{artifact_id}/file")
def download_artifact(
    thread_id: str,
    artifact_id: str,
    preview: bool = False,
    ticket: str | None = None,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> FileResponse:
    """下载/播放模型产出的文件。

    鉴权口径与材料下载一致：Bearer 头或短时票据（浏览器 <audio>/<img> 带不了头）。
    另外会校验文件确实落在该会话的工作区里，防止越权读到别的路径。
    """
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    thread = _thread_or_404(session, thread_id, user)
    artifact = session.get(Artifact, artifact_id)
    if artifact is None or artifact.thread_id != thread.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found")

    settings = get_settings()
    workspace_root = workspace.workspace_dir(settings, user.id, thread.id).resolve()
    raw_path = artifact.stored_path
    # preview=1：给内嵌播放器/缩略图用的浏览器友好副本（没有副本时回退到原文件）
    if preview and artifact.preview_path:
        raw_path = artifact.preview_path
    # 只接受绝对路径：相对路径会跟着进程 CWD 解析，容易指到意料之外的地方
    if not Path(raw_path).is_absolute():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found")
    path = Path(raw_path).resolve()
    if not path.is_file() or not path.is_relative_to(workspace_root):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found")
    filename = path.name if preview and path != Path(artifact.stored_path) else artifact.filename
    return FileResponse(path, filename=filename)
