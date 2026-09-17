"""跨会话引用：会话码、引用解析、索引注入、权限与边界。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import events
from app.api import auth as auth_api
from app.api.threads import resolve_reference_codes
from app.artifacts import is_ignored
from app.auth import hash_password, issue_token
from app.codex_service import context
from app.codex_service.manager import CodexManager
from app.codes import CODE_ALPHABET, extract_references, generate_code, is_valid_code
from app.db import get_engine
from app.main import app as real_app
from app.models import Material, MaterialKind, Message, Thread, User
from app.thread_codes import ensure_code
from pathlib import Path


# --------------------------------------------------------------------------
# 会话码本身
# --------------------------------------------------------------------------
def test_generated_codes_are_safe_and_unique_enough():
    codes = {generate_code() for _ in range(200)}
    assert len(codes) > 190, "4 位码的碰撞率应该很低"
    for code in codes:
        assert len(code) == 4
        assert all(ch in CODE_ALPHABET for ch in code), "不该出现 0/O/1/I/L 之类易混字符"


def test_extract_references_is_case_insensitive_and_ignores_noise():
    text = "请参考 #k7m2 的作文要求，再结合 #B4Q9 和 #K7M2（重复）"
    assert extract_references(text) == ["K7M2", "B4Q9"]
    # 5 位以上长串、以及不带 # 的裸词都不算引用
    assert extract_references("#ABCDEF 和 K7M2") == []
    # 紧跟字母/数字的 # 也不认（避免把 x#1234 这种型号当引用）
    assert extract_references("x#1234") == []
    assert is_valid_code("k7m2") and not is_valid_code("K7M")


def test_thread_code_is_assigned_per_user(engine):
    with Session(engine) as session:
        session.add(User(id="u1", username="a", password_hash="x"))
        session.add(User(id="u2", username="b", password_hash="x"))
        session.commit()
        t1 = Thread(id="t1", user_id="u1")
        t2 = Thread(id="t2", user_id="u1")
        other = Thread(id="t3", user_id="u2")
        session.add_all([t1, t2, other])
        session.commit()
        ensure_code(session, t1)
        ensure_code(session, t2)
        ensure_code(session, other)
        session.commit()
        assert t1.code and t2.code and t1.code != t2.code
        # 幂等：再调一次不会换码
        assert ensure_code(session, t1) == t1.code


# --------------------------------------------------------------------------
# 引用解析（API 层逻辑）
# --------------------------------------------------------------------------
def test_resolve_reference_codes_handles_unknown_and_foreign(engine):
    with Session(engine) as session:
        session.add(User(id="u1", username="a", password_hash="x"))
        session.add(User(id="u2", username="b", password_hash="x"))
        session.commit()
        mine = Thread(id="t1", user_id="u1", code="K7M2")
        current = Thread(id="t2", user_id="u1", code="AAAA")
        foreign = Thread(id="t3", user_id="u2", code="ZZZZ")
        session.add_all([mine, current, foreign])
        session.commit()
        user = session.get(User, "u1")

        ids, unresolved = resolve_reference_codes(
            session, user, "参考 #K7M2 和 #ZZZZ 还有 #QQQQ，以及自己 #AAAA", "t2"
        )
        assert ids == ["t1"]
        assert set(unresolved) == {"ZZZZ", "QQQQ"}  # 别人的会话 = 找不到
        assert "AAAA" not in ids  # 引用自己当前会话无意义，忽略


def test_reference_preview_endpoint(settings, engine):
    """发送前预览接口：返回会引用到的会话与它们各有多少材料/消息。"""
    auth_api.reset_rate_limits()
    with TestClient(real_app) as client:
        with Session(get_engine()) as session:
            user = User(username="preview_user", password_hash=hash_password("goodpass123"))
            session.add(user)
            session.commit()
            session.refresh(user)
            source = Thread(user_id=user.id, title="三年级数学", code="K7M2")
            session.add(source)
            session.commit()
            session.refresh(source)
            session.add(
                Material(
                    thread_id=source.id, filename="作文.pdf", kind=MaterialKind.document,
                    stored_path="/tmp/a.pdf", status="ready",
                )
            )
            session.add_all(
                [
                    Message(thread_id=source.id, role="user", content="帮我看看"),
                    Message(thread_id=source.id, role="assistant", content="归纳：…"),
                ]
            )
            session.commit()
            token = issue_token(session, user)

        headers = {"Authorization": f"Bearer {token}"}
        response = client.post(
            "/api/threads/reference-preview",
            json={"text": "参考 #K7M2 和 #ZZZZ 帮我批改"},
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["unresolved"] == ["ZZZZ"]
        assert len(body["references"]) == 1
        entry = body["references"][0]
        assert entry["code"] == "K7M2" and entry["title"] == "三年级数学"
        assert entry["material_count"] == 1 and entry["message_count"] == 2

        # 列表接口也带上了材料/消息数，供"引用选择器"展示
        listed = client.get("/api/threads", headers=headers).json()
        assert listed[0]["code"] == "K7M2"
        assert listed[0]["material_count"] == 1
        assert listed[0]["message_count"] == 2
    auth_api.reset_rate_limits()


# --------------------------------------------------------------------------
# 注入块
# --------------------------------------------------------------------------
def _fixture_objects():
    thread = Thread(id="t1", user_id="u1", code="K7M2", title="三年级数学作业")
    materials = [
        Material(
            id="m1", thread_id="t1", filename="作文要求.pdf", kind=MaterialKind.document,
            stored_path="/data/users/u1/threads/t1/workspace/x_作文要求.pdf",
            text_path="/data/users/u1/threads/t1/workspace/.derived/m1.md",
            purpose="批改参考",
        ),
        Material(
            id="m2", thread_id="t1", filename="听力.m4a", kind=MaterialKind.audio,
            stored_path="/data/users/u1/threads/t1/workspace/y_听力.m4a",
            text_path="/data/users/u1/threads/t1/workspace/.derived/m2.txt",
        ),
        Material(
            id="m3", thread_id="t1", filename="作业照片.jpg", kind=MaterialKind.image,
            stored_path="/data/users/u1/threads/t1/workspace/z_作业照片.jpg",
        ),
    ]
    messages = [
        Message(id="msg1", thread_id="t1", role="user", content="帮我看下这份作文，要求见附件\n第二行不该出现"),
        Message(
            id="msg2",
            thread_id="t1",
            role="assistant",
            content="归纳：这篇作文结构完整，主要在时态上有点问题。\n细节…",
            result_type="grading",
            grading_result_json=json.dumps(
                {
                    "summary": "共 27 题，答对 23 题",
                    "knowledge_points": [{"name": "时态", "mastery": "weak"}],
                    "questions": [
                        {"index": 17, "question": "q", "student_answer": "a", "correct": False,
                         "correct_answer": "b", "explanation": "e"},
                    ],
                    "suggestions": [],
                }
            ),
        ),
    ]
    return [(thread, materials, messages)]


def test_reference_block_has_index_paths_and_conversation_digest(settings):
    block = context.build_reference_block(_fixture_objects(), message_head_chars=120, max_chars=6000)
    # 会话标识
    assert "# K7M2《三年级数学作业》" in block
    # 材料索引：文件名 + 原文件路径 + 可读文本路径，且不注入正文
    assert "作文要求.pdf" in block and "批改参考" in block
    assert "/workspace/x_作文要求.pdf" in block
    assert "/.derived/m1.md" in block and "/.derived/m2.txt" in block
    assert "（图片已作为图片输入提供）" in block
    # 对话索引：每条取首行
    assert "对话索引（2 条" in block
    assert "[用户] 帮我看下这份作文，要求见附件" in block
    assert "第二行不该出现" not in block, "索引只取首行"
    # 批改结果索引
    assert "批改结果索引" in block and "错题：17" in block
    # 明确告诉模型去按需读取
    assert "不要凭空推测没读过的内容" in block


def test_reference_block_truncates_when_too_long(settings):
    refs = _fixture_objects()
    block = context.build_reference_block(refs, message_head_chars=120, max_chars=200)
    assert len(block) < 400
    assert "已按上限截断" in block


def test_derived_dir_is_not_collected_as_artifact():
    """`.derived/` 里的可读文本不能被登记成产出物。"""
    assert is_ignored(Path(".derived/m1.md")) is True
    assert is_ignored(Path("workspace/.derived/m1.md")) is True
    assert is_ignored(Path("正常文件.mp3")) is False


# --------------------------------------------------------------------------
# manager 注入与记录
# --------------------------------------------------------------------------
class _FakeClient:
    """记录本轮输入，直接回报 turn/completed。"""

    def __init__(self) -> None:
        self.turn_items: list[dict] | None = None
        self.thread_start_params: dict | None = None

    async def thread_start(self, params):
        self.thread_start_params = params
        return SimpleNamespace(thread=SimpleNamespace(id="codex-t1"))

    async def turn_start(self, thread_id, items):
        self.turn_items = items
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
                    "turn": {"id": "codex-turn-1", "status": "completed", "error": None, "items": []},
                }
            ),
        )


async def test_manager_injects_reference_index_and_records_it(engine, settings, monkeypatch):
    with Session(engine) as session:
        session.add(User(id="u1", username="alice", password_hash="x"))
        session.commit()
        source = Thread(id="src", user_id="u1", code="K7M2", title="源会话")
        current = Thread(id="cur", user_id="u1", code="AAAA", title="新会话")
        session.add_all([source, current])
        session.commit()
        session.add(
            Material(
                id="m1", thread_id="src", filename="作文要求.pdf", kind=MaterialKind.document,
                stored_path="/tmp/x.pdf", text_path="/tmp/.derived/m1.md", status="ready",
            )
        )
        session.add(Message(thread_id="src", role="user", content="这是我的作文要求"))
        session.commit()

    manager = CodexManager(settings)
    fake = _FakeClient()
    runtime = SimpleNamespace(client=fake, live_threads=set())

    async def fake_get_runtime(user_id):
        return runtime

    monkeypatch.setattr(manager, "get_runtime", fake_get_runtime)
    queue = events.subscribe("cur")
    await manager._run_turn_locked(
        "u1", "cur", "turn1", "参考 #K7M2 帮我批改", None,
        reference_ids=["src"], unresolved_codes=["QQQQ"],
    )

    # 1) 注入：模型收到的文本里带引用索引（路径 + 对话索引）
    sent_text = fake.turn_items[0]["text"]
    assert "# K7M2《源会话》" in sent_text
    assert "/tmp/.derived/m1.md" in sent_text
    assert "[用户] 这是我的作文要求" in sent_text

    # 2) 记录：用户消息上留下引用记录，前端可以渲染"引用了 #K7M2"
    with Session(engine) as session:
        user_msg = session.exec(
            select(Message).where(Message.thread_id == "cur", Message.role == "user")
        ).one()
        refs = json.loads(user_msg.references_json or "[]")
        assert refs == [{"code": "K7M2", "thread_id": "src", "title": "源会话"}]
        assistant = session.exec(
            select(Message).where(Message.thread_id == "cur", Message.role == "assistant")
        ).one()
        assert "没找到会话 #QQQQ" in assistant.content


async def test_manager_adds_reference_images_up_to_limit(engine, settings, monkeypatch):
    with Session(engine) as session:
        session.add(User(id="u1", username="alice", password_hash="x"))
        session.commit()
        session.add_all(
            [Thread(id="src", user_id="u1", code="K7M2"), Thread(id="cur", user_id="u1", code="AAAA")]
        )
        session.commit()
        for index in range(5):
            session.add(
                Material(
                    id=f"img{index}", thread_id="src", filename=f"照片{index}.jpg",
                    kind=MaterialKind.image, stored_path=f"/tmp/img{index}.jpg", status="ready",
                )
            )
        session.commit()

    manager = CodexManager(settings)
    fake = _FakeClient()
    runtime = SimpleNamespace(client=fake, live_threads=set())

    async def fake_get_runtime(user_id):
        return runtime

    monkeypatch.setattr(manager, "get_runtime", fake_get_runtime)
    await manager._run_turn_locked("u1", "cur", "turn1", "参考 #K7M2", None, reference_ids=["src"])

    images = [item for item in fake.turn_items if item.get("type") == "localImage"]
    assert len(images) == settings.reference_image_limit == 3, "引用图片应限制张数"
    assert [i["path"] for i in images] == ["/tmp/img0.jpg", "/tmp/img1.jpg", "/tmp/img2.jpg"]
