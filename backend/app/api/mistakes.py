"""错题本 API：列表查询、掌握标记、删除、出题练习。"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import get_current_user
from ..codex_service.manager import get_manager
from ..db import get_session
from ..models import Mistake, Thread, User, iso_utc

router = APIRouter(prefix="/api/mistakes", tags=["mistakes"])


class MistakeOut(BaseModel):
    id: str
    knowledge_point: str
    question: str
    student_answer: str
    correct_answer: str
    error_reason: str
    explanation: str
    source_thread_id: str | None
    source_message_id: str | None
    mastered: bool
    created_at: str


class MasteredRequest(BaseModel):
    mastered: bool


class MistakeCreateRequest(BaseModel):
    """手动收藏错题（来自批改卡片）。"""

    knowledge_point: str = ""
    question: str
    student_answer: str = ""
    correct_answer: str = ""
    error_reason: str = ""
    explanation: str = ""
    source_thread_id: str | None = None
    source_message_id: str | None = None


class PracticeResponse(BaseModel):
    thread_id: str


def _mistake_or_404(session: Session, mistake_id: str, user: User) -> Mistake:
    mistake = session.get(Mistake, mistake_id)
    if mistake is None or mistake.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mistake not found")
    return mistake


def _mistake_out(mistake: Mistake) -> MistakeOut:
    return MistakeOut(
        id=mistake.id,
        knowledge_point=mistake.knowledge_point,
        question=mistake.question,
        student_answer=mistake.student_answer,
        correct_answer=mistake.correct_answer,
        error_reason=mistake.error_reason,
        explanation=mistake.explanation,
        source_thread_id=mistake.source_thread_id,
        source_message_id=mistake.source_message_id,
        mastered=mistake.mastered,
        created_at=iso_utc(mistake.created_at),
    )


@router.post("", response_model=MistakeOut)
def create_mistake(
    body: MistakeCreateRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> MistakeOut:
    # 与自动收录同规则去重：同一来源消息下同一题目只留一条
    if body.source_message_id:
        existing = session.exec(
            select(Mistake).where(
                Mistake.user_id == user.id,
                Mistake.source_message_id == body.source_message_id,
                Mistake.question == body.question,
            )
        ).first()
        if existing is not None:
            return _mistake_out(existing)
    mistake = Mistake(
        user_id=user.id,
        knowledge_point=body.knowledge_point,
        question=body.question,
        student_answer=body.student_answer,
        correct_answer=body.correct_answer,
        error_reason=body.error_reason,
        explanation=body.explanation,
        source_thread_id=body.source_thread_id,
        source_message_id=body.source_message_id,
    )
    session.add(mistake)
    session.commit()
    session.refresh(mistake)
    return _mistake_out(mistake)


@router.get("", response_model=list[MistakeOut])
def list_mistakes(
    knowledge_point: str | None = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[MistakeOut]:
    statement = select(Mistake).where(Mistake.user_id == user.id)
    if knowledge_point:
        statement = statement.where(Mistake.knowledge_point == knowledge_point)
    statement = statement.order_by(Mistake.created_at.desc())
    return [_mistake_out(m) for m in session.exec(statement).all()]


@router.post("/{mistake_id}/mastered", response_model=MistakeOut)
def set_mastered(
    mistake_id: str,
    body: MasteredRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> MistakeOut:
    mistake = _mistake_or_404(session, mistake_id, user)
    mistake.mastered = body.mastered
    session.add(mistake)
    session.commit()
    return _mistake_out(mistake)


@router.delete("/{mistake_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mistake(
    mistake_id: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> None:
    mistake = _mistake_or_404(session, mistake_id, user)
    session.delete(mistake)
    session.commit()


_PRACTICE_PROMPT = (
    "孩子之前做错了这道题：题目「{question}」，错误答案「{student_answer}」，"
    "正确答案「{correct_answer}」，错因「{error_reason}」。"
    "请出 2 道同知识点的变式练习题让孩子巩固，只出题不要给答案，语气鼓励。"
    "这次不要写 grading_result.json。"
)


@router.post("/{mistake_id}/practice", response_model=PracticeResponse)
async def practice_mistake(
    mistake_id: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> PracticeResponse:
    """为错题创建练习会话并异步发起一个出题 turn。"""
    mistake = _mistake_or_404(session, mistake_id, user)
    title = f"错题练习：{mistake.knowledge_point}" if mistake.knowledge_point else "错题练习"
    thread = Thread(user_id=user.id, title=title[:50])
    session.add(thread)
    session.commit()

    text = _PRACTICE_PROMPT.format(
        question=mistake.question,
        student_answer=mistake.student_answer,
        correct_answer=mistake.correct_answer,
        error_reason=mistake.error_reason,
    )
    turn_id = uuid.uuid4().hex
    # 用户消息由 manager 在拿到会话锁后落库，此处不重复写入
    asyncio.create_task(get_manager().run_turn(user.id, thread.id, turn_id, text, None))
    return PracticeResponse(thread_id=thread.id)
