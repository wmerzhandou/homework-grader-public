"""安全加固回归测试：文档关闭、安全头、限流、token 过期/登出、下载票据、上传配额。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.api import auth as auth_api
from app.auth import issue_token
from app.db import get_engine
from app.main import app as real_app
from app.models import AuthToken, InviteCode, Material, MaterialKind, Thread, User
from app.security import (
    ensure_app_secret,
    issue_media_ticket,
    verify_media_ticket,
)


@pytest.fixture()
def client(settings, engine):
    auth_api.reset_rate_limits()
    with TestClient(real_app) as test_client:
        yield test_client
    auth_api.reset_rate_limits()


def _create_user(session: Session, username: str, password: str = "goodpass123") -> str:
    """建用户并返回 user_id（返回 id 而不是对象，避免 detached instance）。"""
    from app.auth import hash_password

    user = User(username=username, password_hash=hash_password(password))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user.id


# --------------------------------------------------------------------------
# 攻击面收缩
# --------------------------------------------------------------------------
def test_api_docs_disabled_by_default(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = client.get(path)
        assert response.status_code == 404
    assert client.get("/api/health").json() == {"status": "ok"}


def test_security_headers_present(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


def test_health_requires_no_auth_but_others_do(client):
    assert client.get("/api/threads").status_code == 401
    assert client.get("/api/mistakes").status_code == 401


def test_spa_fallback_does_not_swallow_api_or_dotfiles(client):
    """未知 API 路径与点号路径必须 404，不能被 SPA 回退成 200 HTML。"""
    for path in (
        "/api/tts",
        "/api/nope",
        "/.env",
        "/.git/config",
        "/data/app.db",
        "/backend/app/main.py",
        "/README.md",
    ):
        response = client.get(path)
        assert response.status_code == 404, f"{path} 返回了 {response.status_code}"
        assert "text/html" not in response.headers.get("content-type", "")
    # 正常前端路由（无扩展名）仍然回退到 index.html
    for path in ("/", "/threads/whatever", "/threads/x/report/y", "/mistakes"):
        spa = client.get(path)
        assert spa.status_code == 200, f"{path} 返回了 {spa.status_code}"
        assert "text/html" in spa.headers["content-type"]


# --------------------------------------------------------------------------
# 限流
# --------------------------------------------------------------------------
def test_login_rate_limited(settings, engine, monkeypatch):
    monkeypatch.setenv("AUTH_RATE_LIMIT_ATTEMPTS", "3")
    from app.config import get_settings

    get_settings.cache_clear()
    auth_api.reset_rate_limits()
    with TestClient(real_app) as client:
        with Session(get_engine()) as session:
            _create_user(session, "rate_user")
        codes = [
            client.post(
                "/api/auth/login", json={"username": "rate_user", "password": "wrong-pass"}
            ).status_code
            for _ in range(4)
        ]
        assert codes[:3] == [401, 401, 401]
        assert codes[3] == 429
        # 正确口令也被限流挡住（限流发生在校验之前）
        blocked = client.post(
            "/api/auth/login", json={"username": "rate_user", "password": "goodpass123"}
        )
        assert blocked.status_code == 429
        assert blocked.headers.get("Retry-After")
    auth_api.reset_rate_limits()
    get_settings.cache_clear()


def test_login_success_returns_token_and_resets_counter(settings, engine):
    with TestClient(real_app) as client:
        with Session(get_engine()) as session:
            _create_user(session, "happy_user")
        # 先失败两次，再成功：成功后计数清零，不应被限流
        for _ in range(2):
            client.post(
                "/api/auth/login", json={"username": "happy_user", "password": "nope"}
            )
        ok = client.post(
            "/api/auth/login", json={"username": "happy_user", "password": "goodpass123"}
        )
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["token"] and body["user"]["username"] == "happy_user"
        headers = {"Authorization": f"Bearer {body['token']}"}
        assert client.get("/api/auth/me", headers=headers).status_code == 200
    auth_api.reset_rate_limits()


# --------------------------------------------------------------------------
# 注册校验
# --------------------------------------------------------------------------
def test_register_validates_username_and_password(client):
    with Session(get_engine()) as session:
        session.add(InviteCode(code="ABCD1234"))
        session.commit()
    bad_username = client.post(
        "/api/auth/register",
        json={"username": "a b", "password": "goodpass123", "invite_code": "ABCD1234"},
    )
    assert bad_username.status_code == 400
    short_password = client.post(
        "/api/auth/register",
        json={"username": "validname", "password": "short", "invite_code": "ABCD1234"},
    )
    assert short_password.status_code == 400
    too_long = client.post(
        "/api/auth/register",
        json={"username": "validname", "password": "x" * 200, "invite_code": "ABCD1234"},
    )
    assert too_long.status_code == 400
    ok = client.post(
        "/api/auth/register",
        json={"username": "validname", "password": "goodpass123", "invite_code": "ABCD1234"},
    )
    assert ok.status_code == 200


# --------------------------------------------------------------------------
# token 生命周期
# --------------------------------------------------------------------------
def test_expired_token_rejected(client):
    with Session(get_engine()) as session:
        user_id = _create_user(session, "expired_user")
        session.add(
            AuthToken(
                token="expired-token",
                user_id=user_id,
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        session.commit()
    response = client.get("/api/auth/me", headers={"Authorization": "Bearer expired-token"})
    assert response.status_code == 401


def test_logout_revokes_token(client):
    with Session(get_engine()) as session:
        user_id = _create_user(session, "logout_user")
        token = issue_token(session, session.get(User, user_id))
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_tokens_get_expiry(client):
    with Session(get_engine()) as session:
        user_id = _create_user(session, "ttl_user")
        token = issue_token(session, session.get(User, user_id))
        record = session.get(AuthToken, token)
    assert record is not None and record.expires_at is not None


def test_default_token_ttl_is_one_week(settings, engine):
    assert settings.auth_token_ttl_s == 7 * 24 * 3600
    with Session(get_engine()) as session:
        user_id = _create_user(session, "ttl_len_user")
        token = issue_token(session, session.get(User, user_id))
        record = session.get(AuthToken, token)
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 6.9 * 24 * 3600 < remaining <= 7 * 24 * 3600


# --------------------------------------------------------------------------
# 材料下载票据
# --------------------------------------------------------------------------
def _make_material(settings, user_id: str, *, name: str = "作业.jpg") -> Material:
    from app.codex_service import workspace

    thread = Thread(user_id=user_id, title="票据测试")
    with Session(get_engine()) as session:
        session.add(thread)
        session.commit()
        session.refresh(thread)
        thread_id = thread.id
    ws = workspace.workspace_dir(settings, user_id, thread_id)
    path = ws / "sample.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0test-image")
    with Session(get_engine()) as session:
        material = Material(
            thread_id=thread_id,
            filename=name,
            stored_path=str(path),
            kind=MaterialKind.image,
        )
        session.add(material)
        session.commit()
        session.refresh(material)
        return material


def test_material_download_accepts_ticket_not_raw_token(client, settings):
    with Session(get_engine()) as session:
        user_id = _create_user(session, "media_user")
        token = issue_token(session, session.get(User, user_id))
    material = _make_material(settings, user_id)

    # 长期登录 token 放进 URL 已不再被接受
    assert (
        client.get(f"/api/materials/{material.id}/file?token={token}").status_code == 401
    )
    # Bearer 头仍然可用
    assert (
        client.get(
            f"/api/materials/{material.id}/file",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
        == 200
    )
    # 短时票据可用
    ticket_resp = client.post(
        "/api/auth/media-ticket", headers={"Authorization": f"Bearer {token}"}
    )
    assert ticket_resp.status_code == 200
    body = ticket_resp.json()
    assert body["expires_in"] > 0
    downloaded = client.get(f"/api/materials/{material.id}/file?ticket={body['ticket']}")
    assert downloaded.status_code == 200
    assert downloaded.content.startswith(b"\xff\xd8")


def test_media_ticket_rejects_forgery_and_expiry(settings):
    secret = ensure_app_secret(settings.data_dir, settings.app_secret)
    assert verify_media_ticket(secret, issue_media_ticket(secret, "user-1", 60)) == "user-1"
    # 篡改签名
    ticket = issue_media_ticket(secret, "user-1", 60)
    assert verify_media_ticket(secret, ticket[:-2] + "aa") is None
    # 换成别的密钥签的票
    assert verify_media_ticket(secret, issue_media_ticket("other-secret", "user-1", 60)) is None
    # 已过期
    expired = issue_media_ticket(secret, "user-1", 60, now=time.time() - 120)
    assert verify_media_ticket(secret, expired) is None
    # 结构不对
    assert verify_media_ticket(secret, "garbage") is None


def test_media_ticket_requires_auth(client):
    assert client.post("/api/auth/media-ticket").status_code == 401


# --------------------------------------------------------------------------
# 上传配额
# --------------------------------------------------------------------------
def test_upload_respects_file_count_limit(client):
    with Session(get_engine()) as session:
        user_id = _create_user(session, "quota_user")
        token = issue_token(session, session.get(User, user_id))
    headers = {"Authorization": f"Bearer {token}"}
    thread_id = client.post("/api/threads", json={"title": "配额"}, headers=headers).json()["id"]
    files = [("files", (f"f{i}.txt", b"hello", "text/plain")) for i in range(11)]
    response = client.post(
        f"/api/threads/{thread_id}/materials", files=files, headers=headers
    )
    assert response.status_code == 413


# --------------------------------------------------------------------------
# 邀请码不再每次启动堆叠
# --------------------------------------------------------------------------
def test_invite_codes_seeded_only_when_empty(settings, engine):
    with Session(get_engine()) as session:
        first = auth_api.seed_admin(session, "pw-for-test-1")
        second = auth_api.seed_admin(session, "pw-for-test-1")
    assert len(first) == 3
    assert second == []
    from sqlalchemy import func
    from sqlmodel import select

    with Session(get_engine()) as session:
        unused = session.exec(
            select(func.count()).select_from(InviteCode).where(InviteCode.used_by.is_(None))
        ).one()
    assert unused == 3


def test_data_dir_permissions_restricted(client, settings):
    mode = Path(settings.data_dir).stat().st_mode & 0o777
    assert mode & 0o007 == 0  # 其他用户无任何权限
