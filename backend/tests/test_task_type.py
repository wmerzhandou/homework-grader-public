"""任务类型框架：developerInstructions 按类型分发、结果文件按类型收集、存量库迁移。"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import threads as threads_api
from app.auth import issue_token
from app.codex_service import context, workspace
from app.codex_service.grading import EnglishPassageResult, load_english_result
from app.codex_service.manager import CodexManager
from app.models import Message, Thread, User

ENGLISH_RESULT = {
    "title": "My Family",
    "answers": [{"blank": 1, "answer": "B", "word": "smile", "meaning": "微笑"}],
    "sentences": [
        {"text": "Hi! I'm Mike.", "words": [["Hi!", "你好！"], ["I'm", "我是"], ["Mike", "迈克"]]}
    ],
    "word_cards": [{"word": "smile", "meaning": "微笑"}],
    "notes": ["happy 和 sad 是反义词"],
}


def _load_migration_module():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "migrate_task_type_result_type.py"
    )
    spec = importlib.util.spec_from_file_location("migrate_task_type_result_type", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------
# developer_instructions 按类型分发
# ----------------------------------------------------------------------
class TestDeveloperInstructions:
    def test_grading_instructions_unchanged_and_wrapped(self):
        instructions = context.developer_instructions("grading")
        assert instructions.startswith(context.GRADING_INSTRUCTIONS)
        assert "grading_result.json" in instructions
        assert "knowledge_points" in instructions
        assert "english_result.json" not in instructions

    def test_english_passage_instructions(self):
        instructions = context.developer_instructions("english_passage")
        assert "english_result.json" in instructions
        assert '"word_cards"' in instructions
        assert '"sentences"' in instructions
        assert '"answers"' in instructions
        assert "grading_result.json" not in instructions

    def test_auto_instructions_cover_both_contracts(self):
        instructions = context.developer_instructions("auto")
        assert "grading_result.json" in instructions
        assert "english_result.json" in instructions
        assert "不要写任何结果文件" in instructions

    def test_general_instructions_forbid_result_files(self):
        instructions = context.developer_instructions("general")
        assert "不要写任何结果文件" in instructions

    def test_unknown_task_type_falls_back_to_auto(self):
        assert context.developer_instructions("whatever") == context.developer_instructions("auto")

    @pytest.mark.parametrize("task_type", ["auto", "grading", "english_passage", "general"])
    def test_global_rules_appended_to_all_types(self, task_type: str):
        instructions = context.developer_instructions(task_type)
        assert "全程使用简体中文" in instructions
        assert "HTML" in instructions
        assert instructions.endswith(context.GLOBAL_RULES)


# ----------------------------------------------------------------------
# english_result.json 校验
# ----------------------------------------------------------------------
class TestEnglishPassageResult:
    def test_roundtrip(self):
        result = EnglishPassageResult.model_validate(ENGLISH_RESULT)
        assert result.title == "My Family"
        assert result.answers[0].blank == 1
        assert result.sentences[0].words == [("Hi!", "你好！"), ("I'm", "我是"), ("Mike", "迈克")]
        assert result.word_cards[0].meaning == "微笑"
        assert result.notes == ["happy 和 sad 是反义词"]

    def test_load_valid(self, tmp_path: Path):
        (tmp_path / "english_result.json").write_text(
            json.dumps(ENGLISH_RESULT), encoding="utf-8"
        )
        result = load_english_result(tmp_path)
        assert result is not None
        assert result.title == "My Family"

    def test_load_missing(self, tmp_path: Path):
        assert load_english_result(tmp_path) is None

    def test_load_invalid_json(self, tmp_path: Path):
        (tmp_path / "english_result.json").write_text("not json", encoding="utf-8")
        assert load_english_result(tmp_path) is None

    def test_load_schema_violation(self, tmp_path: Path):
        bad = dict(ENGLISH_RESULT, sentences=[{"text": "Hi", "words": [["only-one-element"]]}])
        (tmp_path / "english_result.json").write_text(json.dumps(bad), encoding="utf-8")
        assert load_english_result(tmp_path) is None


# ----------------------------------------------------------------------
# threads API：task_type 创建/返回/默认值
# ----------------------------------------------------------------------
def _seed_user(engine, username: str = "alice") -> tuple[str, str]:
    with Session(engine) as session:
        user = User(username=username, password_hash="x")
        session.add(user)
        session.commit()
        token = issue_token(session, user)
        return user.id, token


@pytest.fixture()
def client(engine):
    app = FastAPI()
    app.include_router(threads_api.router)
    return TestClient(app)


class TestThreadTaskTypeApi:
    def test_create_with_task_type(self, engine, client):
        _, token = _seed_user(engine)
        resp = client.post(
            "/api/threads",
            json={"title": "英语阅读", "task_type": "english_passage"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["task_type"] == "english_passage"

    def test_create_default_task_type_is_auto(self, engine, client):
        _, token = _seed_user(engine)
        resp = client.post(
            "/api/threads", json={}, headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.json()["task_type"] == "auto"

    def test_create_rejects_unknown_task_type(self, engine, client):
        _, token = _seed_user(engine)
        resp = client.post(
            "/api/threads",
            json={"task_type": "math"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    def test_thread_detail_includes_task_type_and_result_type(self, engine, client):
        user_id, token = _seed_user(engine)
        with Session(engine) as session:
            thread = Thread(user_id=user_id, title="t", task_type="grading")
            session.add(thread)
            session.commit()
            session.add(
                Message(
                    thread_id=thread.id,
                    role="assistant",
                    content="改好了",
                    grading_result_json="{}",
                    result_type="grading",
                )
            )
            session.commit()
            thread_id = thread.id

        resp = client.get(
            f"/api/threads/{thread_id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_type"] == "grading"
        assert body["messages"][0]["result_type"] == "grading"
        assert body["messages"][0]["grading_result"] == {}


# ----------------------------------------------------------------------
# manager：结果文件按类型收集
# ----------------------------------------------------------------------
class _FakeClient:
    """最小化的 codex client：记录 thread_start 参数后直接回报 turn/completed。"""

    def __init__(self, during_turn=None):
        self.thread_start_params: dict | None = None
        # 模拟"模型在本轮里写了文件"：在 turn_start 时执行
        self.during_turn = during_turn

    async def thread_start(self, params):
        self.thread_start_params = params
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


async def _run_turn(
    engine, settings, monkeypatch, *, task_type: str = "auto", during_turn=None
) -> _FakeClient:
    with Session(engine) as session:
        session.add(User(id="u1", username="alice", password_hash="x"))
        session.commit()
        session.add(Thread(id="t1", user_id="u1", task_type=task_type))
        session.commit()

    manager = CodexManager(settings)
    fake_client = _FakeClient(during_turn=during_turn)
    runtime = SimpleNamespace(client=fake_client, live_threads=set())

    async def fake_get_runtime(user_id):
        return runtime

    monkeypatch.setattr(manager, "get_runtime", fake_get_runtime)
    await manager._run_turn_locked("u1", "t1", "turn1", "你好", None)
    return fake_client


def _assistant_message(engine) -> Message:
    with Session(engine) as session:
        return session.exec(select(Message).where(Message.role == "assistant")).one()


async def test_turn_collects_english_result(engine, settings, monkeypatch):
    ws = workspace.workspace_dir(settings, "u1", "t1")
    ws.mkdir(parents=True, exist_ok=True)

    # 结果文件必须是"本轮写出来的"才会被挂到这条回复上
    await _run_turn(
        engine,
        settings,
        monkeypatch,
        task_type="english_passage",
        during_turn=lambda: (ws / "english_result.json").write_text(
            json.dumps(ENGLISH_RESULT), encoding="utf-8"
        ),
    )

    message = _assistant_message(engine)
    assert message.result_type == "english_passage"
    # 英语结果的内容存在 grading_result_json 字段（前端按 result_type 解析）
    assert message.grading_result_json is not None
    assert json.loads(message.grading_result_json)["title"] == ENGLISH_RESULT["title"]


async def test_turn_collects_grading_result_with_result_type(engine, settings, monkeypatch):
    ws = workspace.workspace_dir(settings, "u1", "t1")
    ws.mkdir(parents=True, exist_ok=True)

    await _run_turn(
        engine,
        settings,
        monkeypatch,
        task_type="grading",
        during_turn=lambda: (ws / "grading_result.json").write_text(
            json.dumps(
                {
                    "summary": "不错",
                    "knowledge_points": [],
                    "questions": [],
                    "suggestions": [],
                }
            ),
            encoding="utf-8",
        ),
    )

    message = _assistant_message(engine)
    assert message.result_type == "grading"
    assert message.grading_result_json is not None


async def test_turn_without_result_file_has_null_result_type(engine, settings, monkeypatch):
    await _run_turn(engine, settings, monkeypatch, task_type="general")

    message = _assistant_message(engine)
    assert message.result_type is None
    assert message.grading_result_json is None


async def test_turn_invalid_english_result_ignored(engine, settings, monkeypatch):
    ws = workspace.workspace_dir(settings, "u1", "t1")
    ws.mkdir(parents=True, exist_ok=True)

    await _run_turn(
        engine,
        settings,
        monkeypatch,
        task_type="english_passage",
        during_turn=lambda: (ws / "english_result.json").write_text(
            '{"answers": "oops"}', encoding="utf-8"
        ),
    )

    assert _assistant_message(engine).result_type is None


async def test_thread_start_uses_task_type_instructions(engine, settings, monkeypatch):
    fake_client = await _run_turn(engine, settings, monkeypatch, task_type="english_passage")

    instructions = fake_client.thread_start_params["developerInstructions"]
    assert "english_result.json" in instructions
    assert instructions.endswith(context.GLOBAL_RULES)
    # 产出物指引也要在指令里（否则模型又会输出服务器绝对路径）
    assert "产出文件" in instructions


# ----------------------------------------------------------------------
# 存量库迁移
# ----------------------------------------------------------------------
_OLD_SCHEMA = """
CREATE TABLE thread (
    id VARCHAR NOT NULL PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    codex_thread_id VARCHAR,
    created_at DATETIME NOT NULL
);
CREATE TABLE message (
    id VARCHAR NOT NULL PRIMARY KEY,
    thread_id VARCHAR NOT NULL,
    turn_id VARCHAR,
    role VARCHAR NOT NULL,
    content VARCHAR NOT NULL,
    grading_result_json VARCHAR,
    created_at DATETIME NOT NULL
);
"""


def _make_old_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_OLD_SCHEMA)
        conn.execute(
            "INSERT INTO thread (id, user_id, title, created_at) VALUES ('t1', 'u1', '旧会话', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO message (id, thread_id, role, content, grading_result_json, created_at)"
            " VALUES ('m1', 't1', 'assistant', '改好了', '{}', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO message (id, thread_id, role, content, created_at)"
            " VALUES ('m2', 't1', 'user', '帮我批改', '2026-01-01')"
        )
        conn.commit()
    finally:
        conn.close()


class TestMigration:
    def test_migrate_old_db(self, tmp_path: Path):
        db = tmp_path / "old.db"
        _make_old_db(db)

        module = _load_migration_module()
        module.migrate(db, backup=False)

        conn = sqlite3.connect(db)
        try:
            thread_cols = {row[1] for row in conn.execute("PRAGMA table_info(thread)")}
            message_cols = {row[1] for row in conn.execute("PRAGMA table_info(message)")}
            assert "task_type" in thread_cols
            assert "result_type" in message_cols
            assert conn.execute("SELECT task_type FROM thread WHERE id='t1'").fetchone() == (
                "auto",
            )
            # 存量 grading_result_json 非空的消息回填 'grading'，其余保持 NULL
            assert conn.execute(
                "SELECT result_type FROM message WHERE id='m1'"
            ).fetchone() == ("grading",)
            assert conn.execute(
                "SELECT result_type FROM message WHERE id='m2'"
            ).fetchone() == (None,)
        finally:
            conn.close()

    def test_migrate_creates_backup(self, tmp_path: Path):
        db = tmp_path / "old.db"
        _make_old_db(db)

        module = _load_migration_module()
        module.migrate(db)

        assert (tmp_path / "old.db.bak").is_file()

    def test_migrate_is_idempotent(self, tmp_path: Path):
        db = tmp_path / "old.db"
        _make_old_db(db)

        module = _load_migration_module()
        module.migrate(db, backup=False)
        module.migrate(db, backup=False)  # 第二次运行不应报错或重复回填

        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT task_type FROM thread").fetchall() == [("auto",)]
        finally:
            conn.close()
