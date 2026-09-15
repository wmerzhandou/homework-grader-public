"""Thread CRUD, message history, and turn execution endpoints."""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import get_current_user
from ..codex_service import workspace
from ..codex_service.manager import get_manager
from ..config import get_settings
from ..db import get_session
from ..models import DEFAULT_TASK_TYPE, Material, Message, Thread, User, iso_utc

router = APIRouter(prefix="/api/threads", tags=["threads"])

TaskType = Literal["auto", "grading", "english_passage", "general"]


class ThreadCreateRequest(BaseModel):
    title: str | None = None
    task_type: TaskType | None = None


class ThreadOut(BaseModel):
    id: str
    title: str
    task_type: str
    created_at: str


class MaterialOut(BaseModel):
    id: str
    filename: str
    kind: str
    status: str
    purpose: str = ""
    summary: str = ""
    transcript: str = ""
    error: str | None = None


class MessageOut(BaseModel):
    id: str
    turn_id: str | None
    role: str
    content: str
    grading_result: dict | None = None
    result_type: str | None = None
    materials: list[MaterialOut] = []
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
        task_type=thread.task_type,
        created_at=iso_utc(thread.created_at),
    )


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
    """删除会话：连带删除消息、材料记录和磁盘上的工作区。错题本条目保留。"""
    thread = _thread_or_404(session, thread_id, user)
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
    return [_thread_out(t) for t in threads]


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


@router.get("/{thread_id}", response_model=ThreadDetailOut)
def get_thread(
    thread_id: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ThreadDetailOut:
    thread = _thread_or_404(session, thread_id, user)
    messages = session.exec(
        select(Message).where(Message.thread_id == thread.id).order_by(Message.created_at)
    ).all()
    materials = session.exec(
        select(Material).where(Material.thread_id == thread.id).order_by(Material.created_at)
    ).all()
    materials_by_id = {mat.id: mat for mat in materials}
    return ThreadDetailOut(
        **_thread_out(thread).model_dump(),
        messages=[
            MessageOut(
                id=m.id,
                turn_id=m.turn_id,
                role=m.role,
                content=m.content,
                grading_result=(
                    json.loads(m.grading_result_json) if m.grading_result_json else None
                ),
                result_type=m.result_type,
                materials=[
                    _material_out(materials_by_id[mid])
                    for mid in json.loads(m.material_ids_json or "[]")
                    if mid in materials_by_id
                ],
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
        get_manager().run_turn(user.id, thread.id, turn_id, text, body.material_ids)
    )
    return TurnResponse(turn_id=turn_id)
