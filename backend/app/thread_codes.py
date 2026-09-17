"""会话码的分配：与 codes.py 分开，便于 codes.py 保持纯函数、方便单测。"""

from __future__ import annotations

from sqlmodel import Session, select

from .codes import generate_code, is_valid_code
from .models import Thread


def ensure_code(session: Session, thread: Thread) -> str:
    """给会话分配一个"同一用户内唯一"的码；已有合法码则原样返回（幂等）。"""
    if thread.code and is_valid_code(thread.code):
        return thread.code
    taken = {
        t.code
        for t in session.exec(select(Thread).where(Thread.user_id == thread.user_id)).all()
        if t.id != thread.id
    }
    for _ in range(200):
        code = generate_code()
        if code not in taken:
            thread.code = code
            return code
    raise RuntimeError("会话码分配失败：重试次数过多")
