"""模型产出物：发现规则、结果文件"本轮是否写过"、产出物接口与越权防护。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import auth as auth_api
from app.artifacts import (
    discover,
    is_ignored,
    result_changed,
    result_state,
    snapshot,
)
from app.auth import hash_password, issue_token
from app.db import get_engine
from app.main import app as real_app
from app.models import Artifact, Message, Thread, User


# --------------------------------------------------------------------------
# 发现规则
# --------------------------------------------------------------------------
def test_is_ignored_rules():
    assert is_ignored(Path("grading_result.json")) is True
    assert is_ignored(Path("english_result.json")) is True
    assert is_ignored(Path("keyframes/abc_0.jpg")) is True
    assert is_ignored(Path(".git/config")) is True
    assert is_ignored(Path(".codex/foo")) is True
    assert is_ignored(Path("短文朗读.mp3")) is False


def test_discover_detects_new_and_modified_files(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "原文件.jpg").write_bytes(b"a")
    before = snapshot(ws)

    # 新增：模型生成的音频
    (ws / "朗读.mp3").write_bytes(b"audio")
    # 忽略项：结果文件与隐藏目录
    (ws / "grading_result.json").write_text("{}")
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("x")

    found_files, skipped = discover(ws, before)
    found = {p.name for p in found_files}
    assert found == {"朗读.mp3"}
    assert skipped == []

    # 修改已有文件也算产出物
    after = snapshot(ws)
    time.sleep(0.01)
    (ws / "原文件.jpg").write_bytes(b"bb")
    found_files, _ = discover(ws, after)
    assert {p.name for p in found_files} == {"原文件.jpg"}


def test_discover_respects_exclude_and_size_limit(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    uploaded = ws / "上传的原图.jpg"
    uploaded.write_bytes(b"x")
    before = snapshot(ws, exclude={str(uploaded)})

    (ws / "新图.png").write_bytes(b"y")
    time.sleep(0.01)
    uploaded.write_bytes(b"zzz")  # 上传文件被模型改写也不该登记
    found_files, _ = discover(ws, before, exclude={str(uploaded)})
    found = {p.name for p in found_files}
    assert found == {"新图.png"}


def test_discover_ignores_intermediate_products(tmp_path: Path):
    """逐帧/切片这类中间产物不该进聊天窗口。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    before = snapshot(ws)
    (ws / "frame_0001.png").write_bytes(b"x")
    (ws / "0007.jpg").write_bytes(b"x")
    (ws / "seg03.mp4").write_bytes(b"x")
    (ws / "tmp").mkdir()
    (ws / "tmp" / "scratch.bin").write_bytes(b"x")
    (ws / "最终成片.mp4").write_bytes(b"x")
    found_files, _ = discover(ws, before)
    assert {p.name for p in found_files} == {"最终成片.mp4"}


def test_discover_reports_oversized_instead_of_silently_dropping(tmp_path: Path):
    """超过上限的文件必须被"报出来"，而不是悄悄消失（视频很容易触发）。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    before = snapshot(ws)
    big = ws / "大视频.mp4"
    big.write_bytes(b"0" * 2048)
    found_files, skipped = discover(ws, before, hard_max_bytes=1024)
    assert found_files == []
    assert [p.name for p in skipped] == ["大视频.mp4"]


def test_result_changed_only_when_written(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "grading_result.json").write_text('{"summary": "a"}')
    before = result_state(ws)

    # 没动过 → 不算本轮结果
    assert result_changed(before, ws, "grading_result.json") is False
    # 重写（即使内容相同，mtime 变化也应视为本轮写过）
    time.sleep(0.01)
    (ws / "grading_result.json").write_text('{"summary": "a"}')
    assert result_changed(before, ws, "grading_result.json") is True


# --------------------------------------------------------------------------
# 接口：产出物出现在会话详情里，且只能被会话所有者取走
# --------------------------------------------------------------------------
@pytest.fixture()
def client(settings, engine):
    auth_api.reset_rate_limits()
    with TestClient(real_app) as test_client:
        yield test_client
    auth_api.reset_rate_limits()


def _user(username: str) -> str:
    with Session(get_engine()) as session:
        user = User(username=username, password_hash=hash_password("goodpass123"))
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


def _token(user_id: str) -> str:
    with Session(get_engine()) as session:
        return issue_token(session, session.get(User, user_id))


def test_thread_detail_lists_artifacts_and_download_is_scoped(client, settings):
    from app.codex_service import workspace

    owner_id = _user("artifact_owner")
    other_id = _user("artifact_other")
    owner_token, other_token = _token(owner_id), _token(other_id)

    with Session(get_engine()) as session:
        thread = Thread(user_id=owner_id, title="产出物测试")
        session.add(thread)
        session.commit()
        session.refresh(thread)
        thread_id = thread.id
        message = Message(thread_id=thread_id, role="assistant", content="已生成「朗读.mp3」")
        session.add(message)
        session.commit()
        session.refresh(message)
        message_id = message.id

    ws = workspace.workspace_dir(settings, owner_id, thread_id)
    audio = ws / "朗读.mp3"
    audio.write_bytes(b"ID3fake-audio")
    with Session(get_engine()) as session:
        artifact = Artifact(
            thread_id=thread_id,
            message_id=message_id,
            filename="朗读.mp3",
            stored_path=str(audio),
            kind="audio",
            size=audio.stat().st_size,
        )
        session.add(artifact)
        session.commit()
        artifact_id = artifact.id

    detail = client.get(
        f"/api/threads/{thread_id}", headers={"Authorization": f"Bearer {owner_token}"}
    ).json()
    assert detail["messages"][0]["artifacts"][0]["filename"] == "朗读.mp3"

    ok = client.get(
        f"/api/threads/{thread_id}/artifacts/{artifact_id}/file",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert ok.status_code == 200 and ok.content.startswith(b"ID3")

    # 别人既看不到，也下不到
    assert (
        client.get(
            f"/api/threads/{thread_id}", headers={"Authorization": f"Bearer {other_token}"}
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/threads/{thread_id}/artifacts/{artifact_id}/file",
            headers={"Authorization": f"Bearer {other_token}"},
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/threads/{thread_id}/artifacts/{artifact_id}/file").status_code == 401
    )


def test_artifact_outside_workspace_is_refused(client, settings):
    """stored_path 指向工作区之外时必须拒绝（防越权读文件）。"""
    from app.codex_service import workspace

    owner_id = _user("artifact_pathowner")
    owner_token = _token(owner_id)
    with Session(get_engine()) as session:
        thread = Thread(user_id=owner_id, title="越权测试")
        session.add(thread)
        session.commit()
        session.refresh(thread)
        thread_id = thread.id

    workspace.workspace_dir(settings, owner_id, thread_id)  # 确保目录存在
    outside = settings.data_dir / "outside-secret.txt"
    outside.write_text("top secret")
    with Session(get_engine()) as session:
        artifact = Artifact(
            thread_id=thread_id,
            filename="outside-secret.txt",
            stored_path=str(outside),
            kind="document",
            size=outside.stat().st_size,
        )
        session.add(artifact)
        session.commit()
        artifact_id = artifact.id

    response = client.get(
        f"/api/threads/{thread_id}/artifacts/{artifact_id}/file",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert response.status_code == 404


def test_delete_thread_with_artifacts_works(client, settings):
    """回归：带产出物的会话必须能删掉（曾经因为漏删 artifact 触发外键约束 500）。"""
    from app.codex_service import workspace

    owner_id = _user("delete_owner")
    token = _token(owner_id)
    with Session(get_engine()) as session:
        thread = Thread(user_id=owner_id, title="删除回归")
        session.add(thread)
        session.commit()
        session.refresh(thread)
        thread_id = thread.id

    ws = workspace.workspace_dir(settings, owner_id, thread_id)
    artifact_file = ws / "成片.mp4"
    artifact_file.write_bytes(b"data")
    with Session(get_engine()) as session:
        session.add(
            Artifact(
                thread_id=thread_id,
                filename="成片.mp4",
                stored_path=str(artifact_file),
                kind="video",
                size=4,
            )
        )
        session.commit()

    response = client.delete(
        f"/api/threads/{thread_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 204
    with Session(get_engine()) as session:
        assert session.get(Thread, thread_id) is None
        assert session.exec(select(Artifact).where(Artifact.thread_id == thread_id)).all() == []
    assert not ws.parent.exists()


def test_preview_copy_is_served_separately(client, settings):
    """预览用副本、下载用原文件：两者都能取到，且预览副本优先。"""
    from app.codex_service import workspace

    owner_id = _user("preview_owner")
    token = _token(owner_id)
    with Session(get_engine()) as session:
        thread = Thread(user_id=owner_id, title="预览副本")
        session.add(thread)
        session.commit()
        session.refresh(thread)
        thread_id = thread.id

    ws = workspace.workspace_dir(settings, owner_id, thread_id)
    original = ws / "大图.png"
    original.write_bytes(b"ORIGINAL")
    preview = ws / "大图.web.jpg"
    preview.write_bytes(b"PREVIEW")
    with Session(get_engine()) as session:
        artifact = Artifact(
            thread_id=thread_id,
            filename="大图.png",
            stored_path=str(original),
            preview_path=str(preview),
            kind="image",
            size=8,
        )
        session.add(artifact)
        session.commit()
        artifact_id = artifact.id

    headers = {"Authorization": f"Bearer {token}"}
    got_original = client.get(f"/api/threads/{thread_id}/artifacts/{artifact_id}/file", headers=headers)
    got_preview = client.get(
        f"/api/threads/{thread_id}/artifacts/{artifact_id}/file?preview=1", headers=headers
    )
    assert got_original.content == b"ORIGINAL"
    assert got_preview.content == b"PREVIEW"


def test_artifact_oversized_flag_reported(client, settings):
    from app.codex_service import workspace

    owner_id = _user("oversize_owner")
    token = _token(owner_id)
    with Session(get_engine()) as session:
        thread = Thread(user_id=owner_id, title="超大产出物")
        session.add(thread)
        session.commit()
        session.refresh(thread)
        thread_id = thread.id
        message = Message(thread_id=thread_id, role="assistant", content="生成好了")
        session.add(message)
        session.commit()
        session.refresh(message)
        message_id = message.id

    ws = workspace.workspace_dir(settings, owner_id, thread_id)
    big = ws / "大视频.mp4"
    big.write_bytes(b"0" * 16)
    with Session(get_engine()) as session:
        session.add(
            Artifact(
                thread_id=thread_id,
                message_id=message_id,
                filename="大视频.mp4",
                stored_path=str(big),
                kind="video",
                size=16,
                oversized=True,
            )
        )
        session.commit()

    detail = client.get(
        f"/api/threads/{thread_id}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    artifact = detail["messages"][0]["artifacts"][0]
    assert artifact["oversized"] is True
    assert artifact["has_preview"] is False


def test_quota_counts_artifacts(client, settings, engine, monkeypatch):
    """配额必须把模型产出的文件算进去，否则生成的视频可以无限占盘。"""
    monkeypatch.setenv("USER_STORAGE_QUOTA_BYTES", "10000")  # 10KB 配额，方便触发
    from app.config import get_settings

    get_settings.cache_clear()
    from app.codex_service import workspace

    owner_id = _user("quota_owner")
    token = _token(owner_id)
    headers = {"Authorization": f"Bearer {token}"}
    thread_id = client.post("/api/threads", json={"title": "配额"}, headers=headers).json()["id"]

    # 先放一个 8KB 的"模型产出物"
    ws = workspace.workspace_dir(get_settings(), owner_id, thread_id)
    produced = ws / "成片.mp4"
    produced.write_bytes(b"0" * 8000)
    with Session(get_engine()) as session:
        session.add(
            Artifact(
                thread_id=thread_id,
                filename="成片.mp4",
                stored_path=str(produced),
                kind="video",
                size=8000,
            )
        )
        session.commit()

    # 再上传 5KB 就会超出 10KB 配额
    response = client.post(
        f"/api/threads/{thread_id}/materials",
        files=[("files", ("作业.txt", b"1" * 5000, "text/plain"))],
        headers=headers,
    )
    assert response.status_code == 413
    assert "存储空间" in response.text
    get_settings.cache_clear()
