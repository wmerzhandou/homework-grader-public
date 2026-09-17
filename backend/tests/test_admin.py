"""管理员监控功能测试：权限边界、封禁生效、访问聚合、会话吊销。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.api import auth as auth_api
from app.auth import hash_password, issue_token
from app.db import get_engine
from app.main import app as real_app
from app.models import AccessLog, AuthToken, Device, IpBlock, Thread, User


@pytest.fixture()
def client(settings, engine):
    auth_api.reset_rate_limits()
    with TestClient(real_app) as test_client:
        yield test_client
    auth_api.reset_rate_limits()


def _user(username: str, *, is_admin: bool = False) -> str:
    with Session(get_engine()) as session:
        user = User(
            username=username,
            password_hash=hash_password("goodpass123"),
            is_admin=is_admin,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


def _token(user_id: str) -> str:
    with Session(get_engine()) as session:
        return issue_token(session, session.get(User, user_id))


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# 权限边界
# --------------------------------------------------------------------------
def test_admin_endpoints_reject_non_admin(client):
    member_id = _user("member_user")
    member_token = _token(member_id)
    checks = [
        ("get", "/api/admin/visitors", None),
        ("get", "/api/admin/blocks", None),
        ("get", "/api/admin/sessions", None),
        ("post", "/api/admin/blocks", {"ip": "1.2.3.4"}),
        ("post", "/api/admin/sessions/revoke-all", None),
    ]
    for method, path, payload in checks:
        call = getattr(client, method)
        response = (
            call(path, headers=_headers(member_token), json=payload)
            if payload is not None
            else call(path, headers=_headers(member_token))
        )
        assert response.status_code == 403, f"{method} {path} → {response.status_code}"


def test_admin_endpoints_require_auth(client):
    assert client.get("/api/admin/visitors").status_code == 401
    assert client.get("/api/admin/sessions").status_code == 401


def test_me_exposes_is_admin_flag(client):
    admin_id = _user("flagged_admin", is_admin=True)
    member_id = _user("flagged_member")
    assert client.get("/api/auth/me", headers=_headers(_token(admin_id))).json()["is_admin"] is True
    assert client.get("/api/auth/me", headers=_headers(_token(member_id))).json()["is_admin"] is False


# --------------------------------------------------------------------------
# IP 封禁
# --------------------------------------------------------------------------
def test_block_validation_rejects_dangerous_targets(client):
    admin_token = _token(_user("block_admin", is_admin=True))
    headers = _headers(admin_token)
    assert client.post("/api/admin/blocks", json={"ip": "not-an-ip"}, headers=headers).status_code == 400
    assert client.post("/api/admin/blocks", json={"ip": "127.0.0.1"}, headers=headers).status_code == 400
    # TestClient 的客户端标识是 "testclient"，等价于"封自己"
    assert client.post("/api/admin/blocks", json={"ip": "8.8.8.8"}, headers=headers).status_code == 200


def test_blocked_ip_gets_403(client):
    """封禁必须真的挡住请求：直接写库把当前客户端标识封掉，验证 403 与解封。"""
    with Session(get_engine()) as session:
        session.add(IpBlock(ip="testclient", reason="测试封禁", created_by="tester"))
        session.commit()
    blocked = client.get("/api/health")
    assert blocked.status_code == 403
    assert "封禁" in blocked.text
    with Session(get_engine()) as session:
        session.delete(session.get(IpBlock, "testclient"))
        session.commit()
    assert client.get("/api/health").status_code == 200


def test_block_crud_roundtrip(client):
    admin_token = _token(_user("crud_admin", is_admin=True))
    headers = _headers(admin_token)
    created = client.post(
        "/api/admin/blocks",
        json={"ip": "198.51.100.7", "reason": "可疑扫描", "ttl_hours": 24},
        headers=headers,
    )
    assert created.status_code == 200
    body = created.json()
    assert body["ip"] == "198.51.100.7" and body["expires_at"] is not None
    listed = client.get("/api/admin/blocks", headers=headers).json()
    assert any(b["ip"] == "198.51.100.7" for b in listed)
    assert client.delete("/api/admin/blocks/198.51.100.7", headers=headers).status_code == 204
    assert client.delete("/api/admin/blocks/198.51.100.7", headers=headers).status_code == 404


# --------------------------------------------------------------------------
# 访问来源聚合
# --------------------------------------------------------------------------
def test_visitors_aggregation(client):
    admin_id = _user("visitor_admin", is_admin=True)
    admin_token = _token(admin_id)
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        session.add_all(
            [
                AccessLog(ip="203.0.113.50", method="GET", path="/api/threads", status=200,
                          user_id=admin_id, user_agent="iPhone", created_at=now - timedelta(hours=2)),
                AccessLog(ip="203.0.113.50", method="GET", path="/api/threads", status=200,
                          user_id=admin_id, user_agent="iPhone", created_at=now - timedelta(hours=1)),
                AccessLog(ip="203.0.113.50", method="GET", path="/.env", status=404,
                          user_agent="scanner/1.0", created_at=now),
                AccessLog(ip="198.51.100.30", method="GET", path="/", status=200,
                          user_agent="curl", created_at=now),
            ]
        )
        session.commit()

    payload = client.get("/api/admin/visitors?days=7", headers=_headers(admin_token)).json()
    assert payload["window_days"] == 7
    by_ip = {v["ip"]: v for v in payload["visitors"]}
    scanner = by_ip["203.0.113.50"]
    assert scanner["requests"] == 3
    assert scanner["authenticated"] is True
    assert "visitor_admin" in scanner["usernames"]
    assert scanner["error_count"] == 1
    assert any("GET /api/threads" in p for p in scanner["top_paths"])
    assert by_ip["198.51.100.30"]["authenticated"] is False


def test_visitors_excludes_local_and_assets(client):
    admin_token = _token(_user("noise_admin", is_admin=True))
    with Session(get_engine()) as session:
        session.add(
            AccessLog(ip="203.0.113.77", method="GET", path="/api/threads", status=200)
        )
        session.commit()
    payload = client.get("/api/admin/visitors", headers=_headers(admin_token)).json()
    assert all(v["ip"] != "127.0.0.1" for v in payload["visitors"])


def test_visitors_include_geo_device_and_risk(client):
    """富化字段：归属地、设备画像、数据访问审计、可疑度都要出现在响应里。"""
    admin_id = _user("enrich_admin", is_admin=True)
    admin_token = _token(admin_id)
    now = datetime.now(timezone.utc)
    thread_id = "a" * 32
    material_id = "b" * 32
    with Session(get_engine()) as session:
        session.add(Thread(id=thread_id, user_id=admin_id, title="数学作业"))
        session.add(
            Device(
                id="device-abcdefgh",
                label="iPhone · iOS 18.5 · Safari 18",
                user_agent="iPhone",
                last_ip="203.0.113.88",
            )
        )
        session.add_all(
            [
                AccessLog(ip="203.0.113.88", method="POST", path="/api/auth/login", status=401,
                          created_at=now - timedelta(minutes=5), device_id="device-abcdefgh"),
                AccessLog(ip="203.0.113.88", method="GET", path=f"/api/threads/{thread_id}",
                          status=200, user_id=admin_id, created_at=now - timedelta(minutes=4),
                          device_id="device-abcdefgh", user_agent="iPhone"),
                AccessLog(ip="203.0.113.88", method="GET", path=f"/api/materials/{material_id}/file",
                          status=200, user_id=admin_id, created_at=now - timedelta(minutes=3),
                          device_id="device-abcdefgh"),
            ]
        )
        session.commit()

    payload = client.get("/api/admin/visitors", headers=_headers(admin_token)).json()
    visitor = next(v for v in payload["visitors"] if v["ip"] == "203.0.113.88")
    assert visitor["network_type"] in {"国外网络", "云主机/VPN", "未知", "内网"}
    assert "device-abcdefgh" in visitor["device_ids"]
    assert visitor["known_device"] is True
    assert visitor["logins_failed"] == 1
    assert visitor["threads_viewed"] == ["数学作业"]
    assert any("作业" in m or "已删除" in m for m in visitor["materials_downloaded"])
    assert isinstance(visitor["risk_score"], int)
    assert visitor["risk_flags"]


def test_device_trust_lowers_risk_and_toggle_works(client):
    admin_id = _user("device_admin", is_admin=True)
    admin_token = _token(admin_id)
    headers = _headers(admin_token)
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        session.add(Device(id="trustme-12345678", label="iPhone", last_ip="198.51.100.5"))
        session.add_all(
            [
                AccessLog(ip="198.51.100.5", method="GET", path="/api/threads", status=200,
                          user_id=admin_id, device_id="trustme-12345678", created_at=now),
            ]
        )
        session.commit()

    listed = client.get("/api/admin/devices", headers=headers).json()
    device = next(d for d in listed if d["id"] == "trustme-12345678")
    assert device["trusted"] is False and device["ip_count"] == 1

    before = next(
        v for v in client.get("/api/admin/visitors", headers=headers).json()["visitors"]
        if v["ip"] == "198.51.100.5"
    )
    updated = client.post(
        "/api/admin/devices/trustme-12345678", json={"trusted": True, "note": "爸爸的手机"}, headers=headers
    )
    assert updated.status_code == 200 and updated.json()["trusted"] is True
    after = next(
        v for v in client.get("/api/admin/visitors", headers=headers).json()["visitors"]
        if v["ip"] == "198.51.100.5"
    )
    assert after["trusted_device"] is True
    assert after["risk_score"] < before["risk_score"]
    assert any("可信设备" in f for f in after["risk_flags"])


def test_source_trust_lowers_risk(client):
    """自家网络（例如经由公网回来的家中出口）可以被标记为可信来源。"""
    admin_id = _user("source_admin", is_admin=True)
    admin_token = _token(admin_id)
    headers = _headers(admin_token)
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        session.add_all(
            [
                AccessLog(ip="198.51.100.66", method="GET", path="/api/threads", status=200,
                          user_id=admin_id, created_at=now, user_agent=""),
                AccessLog(ip="198.51.100.66", method="GET", path="/api/auth/login", status=401,
                          created_at=now, user_agent=""),
            ]
        )
        session.commit()

    before = next(
        v for v in client.get("/api/admin/visitors", headers=headers).json()["visitors"]
        if v["ip"] == "198.51.100.66"
    )
    updated = client.post(
        "/api/admin/sources/198.51.100.66", json={"trusted": True, "note": "自家出口"}, headers=headers
    )
    assert updated.status_code == 200
    assert updated.json()["source_trusted"] is True
    after = next(
        v for v in client.get("/api/admin/visitors", headers=headers).json()["visitors"]
        if v["ip"] == "198.51.100.66"
    )
    assert after["source_trusted"] is True
    assert after["risk_score"] < before["risk_score"]
    assert any("可信来源" in f for f in after["risk_flags"])


# --------------------------------------------------------------------------
# 会话（登录设备）
# --------------------------------------------------------------------------
def test_sessions_list_hides_raw_token_and_revokes(client):
    admin_id = _user("sessions_admin", is_admin=True)
    admin_token = _token(admin_id)
    other_token = _token(admin_id)
    headers = _headers(admin_token)

    listed = client.get("/api/admin/sessions", headers=headers)
    assert listed.status_code == 200
    sessions = listed.json()
    # 原始 token 绝不能出现在响应里
    assert admin_token not in listed.text and other_token not in listed.text
    assert any(s["current"] for s in sessions)
    assert all(len(s["id"]) == 12 for s in sessions)

    victim = next(s for s in sessions if not s["current"])
    assert client.delete(f"/api/admin/sessions/{victim['id']}", headers=headers).status_code == 204
    assert client.get("/api/auth/me", headers=_headers(other_token)).status_code == 401
    assert client.get("/api/auth/me", headers=headers).status_code == 200


def test_revoke_all_keeps_current_session(client):
    admin_id = _user("revoke_all_admin", is_admin=True)
    member_id = _user("revoke_all_member")
    admin_token = _token(admin_id)
    member_token = _token(member_id)
    headers = _headers(admin_token)

    response = client.post("/api/admin/sessions/revoke-all", headers=headers)
    assert response.status_code == 200
    assert response.json()["revoked"] == 1
    assert client.get("/api/auth/me", headers=_headers(member_token)).status_code == 401
    assert client.get("/api/auth/me", headers=headers).status_code == 200
