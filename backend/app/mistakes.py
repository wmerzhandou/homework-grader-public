"""错题本收录逻辑：把批改结果中答错的题写入 mistake 表。"""

from __future__ import annotations

from sqlmodel import Session, select

from .codex_service.grading import GradingResult
from .models import Mistake


def _pick_knowledge_point(grading: GradingResult) -> str:
    """为整份批改结果挑一个代表知识点：优先掌握薄弱（weak/poor）的第一个。"""
    for kp in grading.knowledge_points:
        if kp.mastery in ("weak", "poor"):
            return kp.name
    return grading.knowledge_points[0].name if grading.knowledge_points else ""


def record_mistakes(
    session: Session,
    *,
    user_id: str,
    thread_id: str,
    message_id: str,
    grading: GradingResult,
) -> list[Mistake]:
    """把答错的题收录进错题本；同一 source_message_id + question 去重，返回新增条目。"""
    wrong = [q for q in grading.questions if not q.correct]
    if not wrong:
        return []
    existing = {
        m.question
        for m in session.exec(
            select(Mistake).where(Mistake.source_message_id == message_id)
        ).all()
    }
    knowledge_point = _pick_knowledge_point(grading)
    created: list[Mistake] = []
    for q in wrong:
        if q.question in existing:
            continue
        mistake = Mistake(
            user_id=user_id,
            knowledge_point=knowledge_point,
            question=q.question,
            student_answer=q.student_answer,
            correct_answer=q.correct_answer,
            error_reason=q.error_reason or "",
            explanation=q.explanation,
            source_thread_id=thread_id,
            source_message_id=message_id,
        )
        session.add(mistake)
        created.append(mistake)
        existing.add(q.question)
    session.commit()
    for mistake in created:
        # 让返回对象在 session 关闭后仍可读取字段
        session.refresh(mistake)
    return created
