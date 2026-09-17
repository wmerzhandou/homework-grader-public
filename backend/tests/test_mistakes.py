"""错题本：自动收录（含去重）、列表筛选、掌握标记、删除、出题练习。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import mistakes as mistakes_service
from app.api import mistakes as mistakes_api
from app.auth import issue_token
from app.codex_service import workspace
from app.codex_service.grading import GradingResult
from app.codex_service.manager import CodexManager
from app.models import Message, Mistake, Thread, User

GRADING = {
    "summary": "分数加法还需巩固",
    "knowledge_points": [
        {"name": "分数加法", "mastery": "weak"},
        {"name": "通分", "mastery": "good"},
    ],
    "questions": [
        {
            "index": 1,
            "question": "1/2 + 1/3 = ?",
            "student_answer": "2/5",
            "correct": False,
            "correct_answer": "5/6",
            "error_reason": "分子分母直接相加",
            "explanation": "需要先通分再相加",
        },
        {
            "index": 2,
            "question": "1/4 + 1/4 = ?",
            "student_answer": "1/2",
            "correct": True,
            "correct_answer": "1/2",
            "error_reason": None,
            "explanation": "答对了",
        },
        {
            "index": 3,
            "question": "2/3 + 1/6 = ?",
            "student_answer": "3/9",
            "correct": False,
            "correct_answer": "5/6",
            "error_reason": "未通分",
            "explanation": "把 2/3 化成 4/6 再相加",
        },
    ],
    "suggestions": ["多练通分"],
}


def _grading() -> GradingResult:
    return GradingResult.model_validate(GRADING)


def _seed_user(engine, username: str = "alice") -> tuple[str, str]:
    """建一个用户并签发 token，返回 (user_id, token)。"""
    with Session(engine) as session:
        user = User(username=username, password_hash="x")
        session.add(user)
        session.commit()
        token = issue_token(session, user)
        return user.id, token


def _seed_mistake(engine, user_id: str, **kwargs) -> Mistake:
    defaults = {
        "user_id": user_id,
        "knowledge_point": "分数加法",
        "question": "1/2 + 1/3 = ?",
        "student_answer": "2/5",
        "correct_answer": "5/6",
        "error_reason": "分子分母直接相加",
        "explanation": "需要先通分再相加",
        "source_thread_id": "t1",
        "source_message_id": "msg1",
    }
    with Session(engine) as session:
        mistake = Mistake(**(defaults | kwargs))
        session.add(mistake)
        session.commit()
        session.refresh(mistake)
        return mistake


@pytest.fixture()
def client(engine):
    app = FastAPI()
    app.include_router(mistakes_api.router)
    return TestClient(app)


# ----------------------------------------------------------------------
# 自动收录
# ----------------------------------------------------------------------
class TestRecordMistakes:
    def test_collects_only_wrong_questions(self, engine):
        user_id, _ = _seed_user(engine)
        with Session(engine) as session:
            created = mistakes_service.record_mistakes(
                session,
                user_id=user_id,
                thread_id="t1",
                message_id="msg1",
                grading=_grading(),
            )
        assert len(created) == 2
        assert {m.question for m in created} == {"1/2 + 1/3 = ?", "2/3 + 1/6 = ?"}
        first = created[0]
        assert first.user_id == user_id
        assert first.knowledge_point == "分数加法"  # 优先选 weak/poor 的知识点
        assert first.student_answer == "2/5"
        assert first.correct_answer == "5/6"
        assert first.error_reason == "分子分母直接相加"
        assert first.explanation == "需要先通分再相加"
        assert first.source_thread_id == "t1"
        assert first.source_message_id == "msg1"
        assert first.mastered is False

    def test_dedup_same_message_and_question(self, engine):
        user_id, _ = _seed_user(engine)
        with Session(engine) as session:
            mistakes_service.record_mistakes(
                session, user_id=user_id, thread_id="t1", message_id="msg1", grading=_grading()
            )
        with Session(engine) as session:
            again = mistakes_service.record_mistakes(
                session, user_id=user_id, thread_id="t1", message_id="msg1", grading=_grading()
            )
        assert again == []
        with Session(engine) as session:
            assert len(session.exec(select(Mistake)).all()) == 2

    def test_all_correct_records_nothing(self, engine):
        user_id, _ = _seed_user(engine)
        grading = _grading()
        for q in grading.questions:
            q.correct = True
        with Session(engine) as session:
            created = mistakes_service.record_mistakes(
                session, user_id=user_id, thread_id="t1", message_id="msg1", grading=grading
            )
        assert created == []


class _FakeClient:
    """最小化的 codex client：thread_start/turn_start 后直接回报 turn/completed。"""

    def __init__(self, during_turn=None):
        self.during_turn = during_turn

    async def thread_start(self, params):
        return SimpleNamespace(thread=SimpleNamespace(id="codex-t1"))

    async def turn_start(self, thread_id, items):
        if self.during_turn is not None:
            self.during_turn()
        return SimpleNamespace(turn=SimpleNamespace(id="codex-turn-1"))

    def register_turn_notifications(self, turn_id):
        pass

    def unregister_turn_notifications(self, turn_id):
        pass

    async def next_turn_notification(self, turn_id):
        from openai_codex.generated.v2_all import TurnCompletedNotification

        return SimpleNamespace(
            method="turn/completed",
            payload=TurnCompletedNotification.model_validate(
                {
                    "thread_id": "codex-t1",
                    "turn": {
                        "id": "codex-turn-1",
                        "status": "completed",
                        "error": None,
                        "items": [],
                    },
                }
            ),
        )


async def test_run_turn_locked_auto_records_mistakes(engine, settings, monkeypatch):
    """_run_turn_locked 落库批改结果后应自动收录错题。"""
    with Session(engine) as session:
        session.add(User(id="u1", username="alice", password_hash="x"))
        session.commit()
        session.add(Thread(id="t1", user_id="u1"))
        session.commit()
    ws = workspace.workspace_dir(settings, "u1", "t1")
    ws.mkdir(parents=True, exist_ok=True)

    manager = CodexManager(settings)
    # 结果文件必须在本轮里写出，才会被挂到这条回复上
    runtime = SimpleNamespace(
        client=_FakeClient(
            during_turn=lambda: (ws / "grading_result.json").write_text(
                json.dumps(GRADING), encoding="utf-8"
            )
        ),
        live_threads=set(),
    )

    async def fake_get_runtime(user_id):
        return runtime

    monkeypatch.setattr(manager, "get_runtime", fake_get_runtime)

    await manager._run_turn_locked("u1", "t1", "turn1", "帮我批改", None)

    with Session(engine) as session:
        mistakes = session.exec(select(Mistake)).all()
        assert len(mistakes) == 2
        assistant = session.exec(select(Message).where(Message.role == "assistant")).one()
        assert all(m.user_id == "u1" for m in mistakes)
        assert all(m.source_thread_id == "t1" for m in mistakes)
        assert all(m.source_message_id == assistant.id for m in mistakes)
        assert all(m.knowledge_point == "分数加法" for m in mistakes)


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
class TestListMistakes:
    def test_list_only_own_mistakes_desc(self, engine, client):
        user_id, token = _seed_user(engine)
        other_id, _ = _seed_user(engine, "bob")
        _seed_mistake(engine, user_id, id="m1", question="题一")
        _seed_mistake(engine, user_id, id="m2", question="题二")
        _seed_mistake(engine, other_id, id="m3", question="别人的错题")

        resp = client.get("/api/mistakes", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert [m["question"] for m in body] == ["题二", "题一"]  # created_at 倒序
        assert body[0]["mastered"] is False
        assert body[0]["knowledge_point"] == "分数加法"

    def test_filter_by_knowledge_point(self, engine, client):
        user_id, token = _seed_user(engine)
        _seed_mistake(engine, user_id, question="分数题", knowledge_point="分数加法")
        _seed_mistake(engine, user_id, question="几何题", knowledge_point="三角形")

        resp = client.get(
            "/api/mistakes",
            params={"knowledge_point": "三角形"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert [m["question"] for m in resp.json()] == ["几何题"]

    def test_requires_auth(self, engine, client):
        assert client.get("/api/mistakes").status_code == 401


class TestMastered:
    def test_toggle_mastered(self, engine, client):
        user_id, token = _seed_user(engine)
        mistake = _seed_mistake(engine, user_id)

        resp = client.post(
            f"/api/mistakes/{mistake.id}/mastered",
            json={"mastered": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["mastered"] is True
        with Session(engine) as session:
            assert session.get(Mistake, mistake.id).mastered is True

        resp = client.post(
            f"/api/mistakes/{mistake.id}/mastered",
            json={"mastered": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.json()["mastered"] is False

    def test_other_users_mistake_404(self, engine, client):
        _, token = _seed_user(engine)
        other_id, _ = _seed_user(engine, "bob")
        mistake = _seed_mistake(engine, other_id)

        resp = client.post(
            f"/api/mistakes/{mistake.id}/mastered",
            json={"mastered": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


class TestDeleteMistake:
    def test_delete(self, engine, client):
        user_id, token = _seed_user(engine)
        mistake = _seed_mistake(engine, user_id)

        resp = client.delete(
            f"/api/mistakes/{mistake.id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 204
        with Session(engine) as session:
            assert session.get(Mistake, mistake.id) is None

    def test_delete_other_users_mistake_404(self, engine, client):
        _, token = _seed_user(engine)
        other_id, _ = _seed_user(engine, "bob")
        mistake = _seed_mistake(engine, other_id)

        resp = client.delete(
            f"/api/mistakes/{mistake.id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 404
        with Session(engine) as session:
            assert session.get(Mistake, mistake.id) is not None


class TestPractice:
    def test_creates_thread_and_starts_turn(self, engine, client, monkeypatch):
        user_id, token = _seed_user(engine)
        mistake = _seed_mistake(engine, user_id)

        run_turn = AsyncMock()
        monkeypatch.setattr(
            mistakes_api, "get_manager", lambda: SimpleNamespace(run_turn=run_turn)
        )

        resp = client.post(
            f"/api/mistakes/{mistake.id}/practice",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        thread_id = resp.json()["thread_id"]

        with Session(engine) as session:
            thread = session.get(Thread, thread_id)
            assert thread is not None
            assert thread.user_id == user_id
            assert thread.title == "错题练习：分数加法"

        run_turn.assert_called_once()
        call = run_turn.call_args
        assert call.args[0] == user_id
        assert call.args[1] == thread_id
        text = call.args[3]
        assert "1/2 + 1/3 = ?" in text
        assert "2/5" in text
        assert "5/6" in text
        assert "分子分母直接相加" in text
        assert "不要写 grading_result.json" in text
        assert call.args[4] is None

    def test_practice_other_users_mistake_404(self, engine, client, monkeypatch):
        _, token = _seed_user(engine)
        other_id, _ = _seed_user(engine, "bob")
        mistake = _seed_mistake(engine, other_id)
        run_turn = AsyncMock()
        monkeypatch.setattr(
            mistakes_api, "get_manager", lambda: SimpleNamespace(run_turn=run_turn)
        )

        resp = client.post(
            f"/api/mistakes/{mistake.id}/practice",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404
        run_turn.assert_not_called()
