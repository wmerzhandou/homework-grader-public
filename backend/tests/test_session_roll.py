"""会话历史过大时的自愈：换新会话 + 交接摘要 + 413 判定。"""
from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from app.codex_service import manager, workspace
from app.db import get_engine
from app.models import Material, MaterialKind, Message, Thread, User


def _seed(engine, thread_id: str = "t1", user_id: str = "u1") -> None:
    with Session(engine) as session:
        if session.get(User, user_id) is None:
            session.add(User(id=user_id, username="alice", password_hash="x"))
            session.commit()
        session.add(Thread(id=thread_id, user_id=user_id, title="作业"))
        session.commit()
        session.add(
            Message(thread_id=thread_id, role="assistant", content="归纳：数学全对，英语两处要改。")
        )
        session.add(
            Material(
                thread_id=thread_id,
                filename="page1.jpg",
                stored_path="/tmp/page1.jpg",
                kind=MaterialKind.image,
            )
        )
        session.commit()


def test_is_payload_too_large_matches_provider_errors():
    assert manager._is_payload_too_large(RuntimeError("unexpected status 413 Payload Too Large"))
    assert manager._is_payload_too_large(RuntimeError("Failed to buffer the request body: length limit exceeded"))
    assert not manager._is_payload_too_large(RuntimeError("connection reset"))


def test_activity_text_maps_tool_items():
    """协议里 item 类型是驼峰；写错就什么都不显示（曾经踩过）。"""
    assert manager._activity_text({"type": "imageView", "path": "/x.png"}) == (
        "image",
        "正在看图核对",
    )
    assert manager._activity_text({"type": "fileChange"}) == ("write", "正在写文件")
    assert manager._activity_text({"type": "reasoning"}) is None
    crop = {"type": "commandExecution", "command": ["/bin/bash", "-lc", "python3 -c 'Image.open(x).crop(...)'"]}
    assert manager._activity_text(crop)[0] == "image"
    render = {"type": "commandExecution", "command": ['/bin/bash', '-lc', 'ffmpeg -i a -i b out.mp4']}
    assert manager._activity_text(render) == ("render", "正在生成视频")
    assert manager._activity_text({"type": "commandExecution", "command": ["ls", "-la"]})[0] == "command"
    assert manager._activity_text({"type": "userMessage"}) is None


def test_session_roll_needed_by_size(settings, engine):
    _seed(engine)
    home = workspace.codex_home(settings, "u1")
    session_dir = home / "sessions" / "2026" / "09" / "17"
    session_dir.mkdir(parents=True, exist_ok=True)
    rollout = session_dir / "rollout-x-abc123.jsonl"
    rollout.write_bytes(b"0" * 1024)  # 1KB

    assert manager._session_roll_needed(settings, "u1", "abc123") is False
    rollout.write_bytes(b"0" * (int(settings.codex_session_max_mb * 1024 * 1024) + 1024))
    assert manager._session_roll_needed(settings, "u1", "abc123") is True
    assert manager._session_roll_needed(settings, "u1", "missing") is False


def test_handoff_prefix_carries_conclusions_and_materials(settings, engine):
    _seed(engine)

    text = manager._handoff_prefix("t1")

    assert "系统交接" in text
    assert "数学全对" in text
    assert "page1.jpg" in text
    assert "不要再把整页图反复裁来裁去" in text


def test_reset_codex_session_clears_binding(settings, engine):
    _seed(engine)
    with Session(engine) as session:
        thread = session.get(Thread, "t1")
        thread.codex_thread_id = "abc123"
        session.add(thread)
        session.commit()

    manager._reset_codex_session("t1")

    with Session(engine) as session:
        assert session.get(Thread, "t1").codex_thread_id is None
