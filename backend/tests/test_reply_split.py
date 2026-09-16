"""最终答复与过程叙述分开：对话只拿 final_answer，过程进 process_log。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlmodel import Session, select

from app import events
from app.codex_service.manager import CodexManager
from app.models import Message, Thread, User
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    AgentMessageDeltaNotification,
    ItemCompletedNotification,
    ItemStartedNotification,
    MessagePhase,
    TurnCompletedNotification,
)


def _started(item_id: str, phase: MessagePhase):
    # 直接用具体类型构造：原始 dict 过 ThreadItem 联合类型不会解析出 phase
    item = AgentMessageThreadItem.model_construct(
        id=item_id,
        type="agentMessage",
        text="",
        phase=phase,
        delivery=None,
        memory_citation=None,
        questions=None,
    )
    return SimpleNamespace(
        method="item/started",
        payload=ItemStartedNotification.model_construct(
            item=item,
            started_at_ms=1,
            thread_id="codex-t1",
            turn_id="codex-turn-1",
        ),
    )


def _delta(item_id: str, text: str):
    return SimpleNamespace(
        method="item/agentMessage/delta",
        payload=AgentMessageDeltaNotification.model_validate(
            {"delta": text, "itemId": item_id, "threadId": "codex-t1", "turnId": "codex-turn-1"}
        ),
    )


def _completed_item(item_id: str, phase: MessagePhase, text: str):
    item = AgentMessageThreadItem.model_construct(
        id=item_id,
        type="agentMessage",
        text=text,
        phase=phase,
        delivery=None,
        memory_citation=None,
        questions=None,
    )
    return SimpleNamespace(
        method="item/completed",
        payload=ItemCompletedNotification.model_construct(
            item=item,
            completed_at_ms=2,
            thread_id="codex-t1",
            turn_id="codex-turn-1",
        ),
    )


def _completed():
    return SimpleNamespace(
        method="turn/completed",
        payload=TurnCompletedNotification.model_validate(
            {
                "thread_id": "codex-t1",
                "turn": {"id": "codex-turn-1", "status": "completed", "error": None, "items": []},
            }
        ),
    )


class _SplitFakeClient:
    def __init__(self) -> None:
        self._script = [
            _started("item-commentary", MessagePhase.commentary),
            _delta("item-commentary", "我先放大看一下图片。"),
            _delta("item-commentary", "现在开始批改。"),
            _completed_item(
                "item-commentary", MessagePhase.commentary, "我先放大看一下图片。现在开始批改。"
            ),
            _started("item-final", MessagePhase.final_answer),
            _delta("item-final", "归纳：全都对了"),
            _delta("item-final", "，继续保持。\n---\n详细内容：第 1~4 题全对。"),
            _completed_item(
                "item-final",
                MessagePhase.final_answer,
                "归纳：全都对了，继续保持。\n---\n详细内容：第 1~4 题全对。",
            ),
            _completed(),
        ]

    async def thread_start(self, params):
        return SimpleNamespace(thread=SimpleNamespace(id="codex-t1"))

    async def turn_start(self, thread_id, items):
        return SimpleNamespace(turn=SimpleNamespace(id="codex-turn-1"))

    def register_turn_notifications(self, turn_id):
        pass

    def unregister_turn_notifications(self, turn_id):
        pass

    async def next_turn_notification(self, turn_id):
        return self._script.pop(0)


async def test_turn_splits_final_answer_and_process(engine, settings, monkeypatch):
    with Session(engine) as session:
        session.add(User(id="u1", username="alice", password_hash="x"))
        session.commit()
        session.add(Thread(id="t1", user_id="u1"))
        session.commit()

    manager = CodexManager(settings)
    runtime = SimpleNamespace(client=_SplitFakeClient(), live_threads=set())

    async def fake_get_runtime(user_id):
        return runtime

    monkeypatch.setattr(manager, "get_runtime", fake_get_runtime)

    queue = events.subscribe("t1")
    await manager._run_turn_locked("u1", "t1", "turn1", "帮我批改", None)

    published: list[str] = []
    while not queue.empty():
        published.append(queue.get_nowait())

    with Session(engine) as session:
        message = session.exec(select(Message).where(Message.role == "assistant")).one()

    # 最终答复只含 final_answer；过程单独存
    assert "归纳：全都对了" in message.content
    assert "我先放大看一下图片" not in message.content
    assert message.process_log is not None and "我先放大看一下图片" in message.process_log

    # 实时流也分开：过程走 process_delta，最终答复走 message_delta
    assert any("process_delta" in p and "我先" in p for p in published)
    assert any("message_delta" in p and "归纳" in p for p in published)
    assert not any("message_delta" in p and "我先" in p for p in published)
    # 过程整条完成后有 process_completed 事件（详情页用它记录过程）
    assert any("process_completed" in p for p in published)


async def test_turn_without_final_answer_falls_back_to_process(engine, settings, monkeypatch):
    """极端情况：只有过程没有最终答复时不能给用户空白。"""
    with Session(engine) as session:
        session.add(User(id="u1", username="alice", password_hash="x"))
        session.commit()
        session.add(Thread(id="t1", user_id="u1"))
        session.commit()

    class _OnlyCommentary(_SplitFakeClient):
        def __init__(self) -> None:
            self._script = [
                _started("item-commentary", MessagePhase.commentary),
                _delta("item-commentary", "只有过程，没有最终答复。"),
                _completed_item(
                    "item-commentary", MessagePhase.commentary, "只有过程，没有最终答复。"
                ),
                _completed(),
            ]

    manager = CodexManager(settings)
    runtime = SimpleNamespace(client=_OnlyCommentary(), live_threads=set())

    async def fake_get_runtime(user_id):
        return runtime

    monkeypatch.setattr(manager, "get_runtime", fake_get_runtime)
    await manager._run_turn_locked("u1", "t1", "turn1", "在吗", None)

    with Session(engine) as session:
        message = session.exec(select(Message).where(Message.role == "assistant")).one()
    assert "只有过程" in message.content
