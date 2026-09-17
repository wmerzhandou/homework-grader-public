"""管理员监控接口：访问来源统计、IP 封禁、登录设备（会话）管理。

全部挂在 /api/admin 下，由 require_admin 守卫；非 admin 账号一律 403。
注意：会话列表只返回 token 的哈希指纹（前 12 位），绝不下发原始 token。
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..access import active_block, client_ip, is_loopback, is_valid_ip, prune_access_log
from ..auth import get_current_admin  # noqa: F401  (供依赖注入)
from ..config import get_settings
from ..db import get_session
from ..geoip import cached_lookup
from ..insights import audit_logs, parse_ua, risk_level, score_source
from ..models import (
    AccessLog,
    AuthToken,
    Device,
    IpBlock,
    IpGeo,
    Material,
    Thread,
    User,
    iso_utc,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

_MAX_ROWS = 50_000  # 单次聚合扫描上限，避免表变大后拖垮接口


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class VisitorOut(BaseModel):
    ip: str
    requests: int
    first_seen: str
    last_seen: str
    authenticated: bool
    usernames: list[str]
    user_agents: list[str]
    top_paths: list[str]
    error_count: int
    blocked: bool
    block_reason: str | None = None
    block_expires_at: str | None = None
    # ---- 归属地与网络 ----
    location: str = ""
    org: str = ""
    asn: int | None = None
    network_type: str = ""
    # ---- 设备画像 ----
    device_label: str = ""
    device_ids: list[str] = []
    trusted_device: bool = False
    known_device: bool = False
    is_bot: bool = False
    # ---- 数据访问审计 ----
    threads_viewed: list[str] = []
    materials_downloaded: list[str] = []
    uploads: int = 0
    turns: int = 0
    logins_ok: int = 0
    logins_failed: int = 0
    sensitive_probes: list[str] = []
    # ---- 可疑度 ----
    risk_score: int = 0
    risk_level: str = "低"
    risk_flags: list[str] = []
    source_trusted: bool = False


class DeviceOut(BaseModel):
    id: str
    label: str
    user_agent: str
    first_seen: str
    last_seen: str
    last_ip: str
    trusted: bool
    note: str
    request_count: int
    ip_count: int


class DeviceUpdateRequest(BaseModel):
    trusted: bool | None = None
    note: str | None = None


class SourceUpdateRequest(BaseModel):
    trusted: bool | None = None
    note: str | None = None


class VisitorsResponse(BaseModel):
    window_days: int
    generated_at: str
    total_records: int
    visitors: list[VisitorOut]


class BlockRequest(BaseModel):
    ip: str
    reason: str = ""
    ttl_hours: int | None = None


class BlockOut(BaseModel):
    ip: str
    reason: str
    created_by: str
    created_at: str
    expires_at: str | None = None


class SessionOut(BaseModel):
    id: str
    username: str
    user_id: str
    created_at: str
    expires_at: str | None
    current: bool


def _block_out(block: IpBlock) -> BlockOut:
    return BlockOut(
        ip=block.ip,
        reason=block.reason,
        created_by=block.created_by,
        created_at=iso_utc(block.created_at),
        expires_at=iso_utc(block.expires_at) if block.expires_at else None,
    )


@router.get("/visitors", response_model=VisitorsResponse)
def visitors(
    days: int = Query(default=7, ge=1, le=90),
    only_suspicious: bool = Query(default=False),
    admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> VisitorsResponse:
    """按 IP 聚合访问来源：归属地、设备画像、数据访问审计、可疑度。"""
    settings = get_settings()
    prune_access_log(session, settings.access_log_retention_days)

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = list(
        session.exec(
            select(AccessLog)
            .where(AccessLog.created_at >= since)
            .order_by(AccessLog.created_at.desc())
            .limit(_MAX_ROWS)
        ).all()
    )
    usernames = {u.id: u.username for u in session.exec(select(User)).all()}
    blocks = {b.ip: b for b in session.exec(select(IpBlock)).all()}
    threads = {t.id: t.title for t in session.exec(select(Thread)).all()}
    material_names = {
        m.id: (m.filename, threads.get(m.thread_id, ""))
        for m in session.exec(select(Material)).all()
    }
    devices = {d.id: d for d in session.exec(select(Device)).all()}

    # 先按 IP 分组（一次遍历），再做归属查询与打分
    grouped: dict[str, dict] = defaultdict(
        lambda: {
            "rows": [],
            "first": None,
            "last": None,
            "users": Counter(),
            "uas": Counter(),
            "paths": Counter(),
            "devices": set(),
            "errors": 0,
        }
    )
    for row in rows:
        item = grouped[row.ip]
        item["rows"].append(row)
        created = _aware(row.created_at)
        if item["first"] is None or created < item["first"]:
            item["first"] = created
        if item["last"] is None or created > item["last"]:
            item["last"] = created
        if row.user_id:
            item["users"][usernames.get(row.user_id, "已删除用户")] += 1
        if row.user_agent:
            item["uas"][row.user_agent] += 1
        if row.device_id:
            item["devices"].add(row.device_id)
        item["paths"][f"{row.method} {row.path}"] += 1
        if row.status >= 400:
            item["errors"] += 1

    # "新来源" = 最近 24 小时内才第一次出现
    new_source_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    visitors: list[VisitorOut] = []
    for ip, item in grouped.items():
        block = blocks.get(ip)
        geo = cached_lookup(session, ip)
        top_ua = item["uas"].most_common(1)[0][0] if item["uas"] else ""
        ua_info = parse_ua(top_ua)
        summary = audit_logs(
            item["rows"], thread_titles=threads, material_names=material_names
        )
        device_ids = sorted(item["devices"])
        trusted = any(devices[d].trusted for d in device_ids if d in devices)
        known = any(d in devices for d in device_ids)
        geo_row = session.get(IpGeo, ip)
        source_trusted = bool(geo_row and geo_row.trusted)
        new_source = bool(item["first"] and item["first"] >= new_source_cutoff)
        score, flags = score_source(
            requests=len(item["rows"]),
            authenticated=bool(item["users"]),
            usernames=[name for name, _ in item["users"].most_common(3)],
            logins_failed=summary.logins_failed,
            sensitive_probes=summary.sensitive_probes,
            not_found=summary.not_found,
            materials_downloaded=len(summary.materials_downloaded),
            network_type=geo.network_type,
            ua_is_bot=ua_info.is_bot,
            has_ua=bool(top_ua),
            device_known=known,
            device_trusted=trusted,
            source_trusted=source_trusted,
            is_new_source=new_source,
        )
        visitors.append(
            VisitorOut(
                ip=ip,
                requests=len(item["rows"]),
                first_seen=item["first"].isoformat(),
                last_seen=item["last"].isoformat(),
                authenticated=bool(item["users"]),
                usernames=[name for name, _ in item["users"].most_common(3)],
                user_agents=[ua for ua, _ in item["uas"].most_common(3)],
                top_paths=[p for p, _ in item["paths"].most_common(5)],
                error_count=item["errors"],
                blocked=bool(block and active_block(session, ip) is not None),
                block_reason=block.reason if block else None,
                block_expires_at=iso_utc(block.expires_at) if block and block.expires_at else None,
                location=geo.location,
                org=geo.org,
                asn=geo.asn,
                network_type=geo.network_type,
                device_label=ua_info.label,
                device_ids=device_ids,
                trusted_device=trusted,
                known_device=known,
                is_bot=ua_info.is_bot,
                threads_viewed=summary.threads_viewed[:20],
                materials_downloaded=summary.materials_downloaded[:20],
                uploads=summary.uploads,
                turns=summary.turns,
                logins_ok=summary.logins_ok,
                logins_failed=summary.logins_failed,
                sensitive_probes=summary.sensitive_probes[:10],
                risk_score=score,
                risk_level=risk_level(score),
                risk_flags=flags,
                source_trusted=source_trusted,
            )
        )
    # 可疑度高的排前面，同级按最近访问时间倒序
    visitors.sort(key=lambda v: (v.risk_score, v.last_seen), reverse=True)
    if only_suspicious:
        visitors = [v for v in visitors if v.risk_score >= 30]
    return VisitorsResponse(
        window_days=days,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_records=len(rows),
        visitors=visitors,
    )


@router.get("/blocks", response_model=list[BlockOut])
def list_blocks(
    admin: User = Depends(get_current_admin), session: Session = Depends(get_session)
) -> list[BlockOut]:
    return [_block_out(b) for b in session.exec(select(IpBlock)).all()]


@router.post("/blocks", response_model=BlockOut)
def create_block(
    body: BlockRequest,
    request: Request,
    admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> BlockOut:
    ip = body.ip.strip()
    if not is_valid_ip(ip):
        raise HTTPException(status_code=400, detail="IP 格式不正确")
    if is_loopback(ip):
        raise HTTPException(status_code=400, detail="不能封禁回环地址（会把自己锁在外面）")
    if ip == client_ip(request):
        raise HTTPException(
            status_code=400, detail="不能封禁你自己当前使用的 IP（会立刻把自己锁在外面）"
        )
    existing = session.get(IpBlock, ip)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=body.ttl_hours)
        if body.ttl_hours
        else None
    )
    if existing is None:
        existing = IpBlock(
            ip=ip,
            reason=body.reason,
            created_by=admin.username,
            expires_at=expires_at,
        )
    else:
        existing.reason = body.reason
        existing.created_by = admin.username
        existing.created_at = datetime.now(timezone.utc)
        existing.expires_at = expires_at
    session.add(existing)
    session.commit()
    session.refresh(existing)
    return _block_out(existing)


@router.delete("/blocks/{ip}", status_code=status.HTTP_204_NO_CONTENT)
def delete_block(
    ip: str,
    admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> None:
    block = session.get(IpBlock, ip)
    if block is None:
        raise HTTPException(status_code=404, detail="该 IP 不在封禁名单里")
    session.delete(block)
    session.commit()


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(
    authorization: str | None = Header(default=None),
    admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> list[SessionOut]:
    """登录设备（会话）列表：只给指纹，不下发原始 token。"""
    current_fp = (
        _fingerprint(authorization.removeprefix("Bearer ").strip())
        if authorization and authorization.startswith("Bearer ")
        else None
    )
    usernames = {u.id: u.username for u in session.exec(select(User)).all()}
    out: list[SessionOut] = []
    for token in session.exec(select(AuthToken)).all():
        fp = _fingerprint(token.token)
        out.append(
            SessionOut(
                id=fp,
                username=usernames.get(token.user_id, "已删除用户"),
                user_id=token.user_id,
                created_at=iso_utc(token.created_at),
                expires_at=iso_utc(token.expires_at) if token.expires_at else None,
                current=fp == current_fp,
            )
        )
    out.sort(key=lambda s: s.created_at, reverse=True)
    return out


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session(
    session_id: str,
    admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> None:
    """按指纹吊销一个会话（踢掉某台设备）。"""
    matches = [
        t for t in session.exec(select(AuthToken)).all() if _fingerprint(t.token).startswith(session_id)
    ]
    if not matches:
        raise HTTPException(status_code=404, detail="没有匹配的会话")
    if len(matches) > 1:
        raise HTTPException(status_code=400, detail="指纹不唯一，请提供更长的前缀")
    session.delete(matches[0])
    session.commit()


class RevokeAllOut(BaseModel):
    revoked: int


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(
    admin: User = Depends(get_current_admin), session: Session = Depends(get_session)
) -> list[DeviceOut]:
    """设备列表：由前端随机 ID 标识，可标记可信（家人设备）。"""
    counts: dict[str, int] = defaultdict(int)
    ips: dict[str, set[str]] = defaultdict(set)
    for row in session.exec(select(AccessLog)).all():
        if row.device_id:
            counts[row.device_id] += 1
            ips[row.device_id].add(row.ip)
    devices = list(session.exec(select(Device)).all())
    devices.sort(key=lambda d: (_aware(d.last_seen), d.id), reverse=True)
    return [
        DeviceOut(
            id=d.id,
            label=d.label,
            user_agent=d.user_agent,
            first_seen=iso_utc(d.first_seen),
            last_seen=iso_utc(d.last_seen),
            last_ip=d.last_ip,
            trusted=d.trusted,
            note=d.note,
            request_count=counts.get(d.id, 0),
            ip_count=len(ips.get(d.id, set())),
        )
        for d in devices
    ]


@router.post("/sources/{ip}", response_model=VisitorOut)
def update_source(
    ip: str,
    body: SourceUpdateRequest,
    admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> VisitorOut:
    """把某个来源（IP）标记为可信（自家网络/公司出口），打分时降权。"""
    if not is_valid_ip(ip):
        raise HTTPException(status_code=400, detail="IP 格式不正确")
    row = session.get(IpGeo, ip)
    if row is None:
        # 该 IP 还没被查询过归属，先建一条缓存记录
        geo = cached_lookup(session, ip)
        row = session.get(IpGeo, ip) or IpGeo(
            ip=ip,
            country=geo.country,
            province=geo.province,
            city=geo.city,
            asn=geo.asn,
            org=geo.org,
            network_type=geo.network_type,
            resolved_at=datetime.now(timezone.utc),
        )
    if body.trusted is not None:
        row.trusted = body.trusted
    if body.note is not None:
        row.note = body.note[:200]
    session.add(row)
    session.commit()
    # 直接复用聚合逻辑返回最新状态
    payload = visitors(days=7, only_suspicious=False, admin=admin, session=session)
    for visitor in payload.visitors:
        if visitor.ip == ip:
            return visitor
    raise HTTPException(status_code=404, detail="该来源在最近 7 天内没有访问记录")


@router.post("/devices/{device_id}", response_model=DeviceOut)
def update_device(
    device_id: str,
    body: DeviceUpdateRequest,
    admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> DeviceOut:
    """标记设备为可信/不可信，或写备注。"""
    device = session.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    if body.trusted is not None:
        device.trusted = body.trusted
    if body.note is not None:
        device.note = body.note[:200]
    session.add(device)
    session.commit()
    session.refresh(device)
    counts = defaultdict(int)
    ips = defaultdict(set)
    for row in session.exec(select(AccessLog)).all():
        if row.device_id == device_id:
            counts[device_id] += 1
            ips[device_id].add(row.ip)
    return DeviceOut(
        id=device.id,
        label=device.label,
        user_agent=device.user_agent,
        first_seen=iso_utc(device.first_seen),
        last_seen=iso_utc(device.last_seen),
        last_ip=device.last_ip,
        trusted=device.trusted,
        note=device.note,
        request_count=counts.get(device_id, 0),
        ip_count=len(ips.get(device_id, set())),
    )


@router.post("/sessions/revoke-all", response_model=RevokeAllOut)
def revoke_all_sessions(
    authorization: str | None = Header(default=None),
    admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> RevokeAllOut:
    """吊销除"当前这个会话"之外的所有会话（所有账号），用于"怀疑泄露就全部换票"。"""
    current = (
        authorization.removeprefix("Bearer ").strip()
        if authorization and authorization.startswith("Bearer ")
        else None
    )
    victims = [t for t in session.exec(select(AuthToken)).all() if t.token != current]
    for token in victims:
        session.delete(token)
    session.commit()
    return RevokeAllOut(revoked=len(victims))
