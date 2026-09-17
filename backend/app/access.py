"""访问记录与 IP 封禁：中间件 + 聚合查询 + 清理。

设计取舍：
- 只记录"有意义的访问"（页面加载与 /api/*），静态资源/健康检查/本机回环/管理页自身不记，
  否则一天几千条静态请求会把表撑满、把真正可疑的来源埋掉。
- 查询串一律不写库：材料下载的 ?ticket= 属于凭据，不能沉淀到数据库里。
- 封禁在中间件里最先判断（早于鉴权），被封的 IP 直接 403，连接不会进入业务逻辑。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, delete, func, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .db import get_engine
from .insights import parse_ua
from .models import AccessLog, AuthToken, Device, IpBlock

logger = logging.getLogger(__name__)

# 不记录的路径前缀/后缀
_SKIP_PREFIXES = ("/api/admin", "/api/health", "/assets/")
_SKIP_SUFFIXES = (
    ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".map", ".webmanifest", ".txt",
)
# 本机回环：看门狗每 30 秒探测一次，记下来只会污染统计
_LOCAL_IPS = {"127.0.0.1", "::1", "localhost", "testclient"}

_UA_MAX = 300
# X-Client-Id：前端生成的随机设备标识（只接受安全的短字符串）
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_DEVICE_TOUCH_INTERVAL_S = 300.0
_device_touched: dict[str, float] = {}


def client_id_from_request(request: Request) -> str:
    raw = (request.headers.get("x-client-id") or "").strip()
    return raw if _CLIENT_ID_RE.fullmatch(raw) else ""


def client_ip(request: Request) -> str:
    """取客户端 IP（与限流共用同一口径：只信直连地址，不信 X-Forwarded-For）。"""
    return request.client.host if request.client else "unknown"


def should_record(ip: str, path: str) -> bool:
    if ip in _LOCAL_IPS:
        return False
    if path.startswith(_SKIP_PREFIXES) or path.endswith(_SKIP_SUFFIXES):
        return False
    return True


def _user_id_from_request(session: Session, request: Request) -> str | None:
    """从 Authorization 头解析出用户（只做 PK 查询，不校验过期——过期与否不影响"是谁访问"）。"""
    header = request.headers.get("authorization") or ""
    if not header.startswith("Bearer "):
        return None
    record = session.get(AuthToken, header.removeprefix("Bearer ").strip())
    return record.user_id if record else None


def active_block(session: Session, ip: str) -> IpBlock | None:
    block = session.get(IpBlock, ip)
    if block is None:
        return None
    if block.expires_at is None:
        return block
    expires_at = block.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return block if expires_at > datetime.now(timezone.utc) else None


class AccessControlMiddleware(BaseHTTPMiddleware):
    """封禁拦截（最早执行）+ 访问记录（响应之后）。"""

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        ip = client_ip(request)
        path = request.url.path

        with Session(get_engine()) as session:
            block = active_block(session, ip)
        if block is not None:
            if should_record(ip, path):
                _record(ip, request, path, 403, None)
            return JSONResponse(
                {"detail": "该设备已被封禁，如有疑问请联系管理员"},
                status_code=403,
            )

        response = await call_next(request)
        if should_record(ip, path):
            with Session(get_engine()) as session:
                user_id = _user_id_from_request(session, request)
            _record(ip, request, path, response.status_code, user_id)
        return response


def _record(ip: str, request: Request, path: str, status: int, user_id: str | None) -> None:
    """写一条访问记录；失败绝不影响正常请求。"""
    try:
        device_id = client_id_from_request(request)
        with Session(get_engine()) as session:
            session.add(
                AccessLog(
                    ip=ip,
                    method=request.method,
                    path=path,
                    status=status,
                    user_id=user_id,
                    user_agent=(request.headers.get("user-agent") or "")[:_UA_MAX],
                    device_id=device_id,
                )
            )
            session.commit()
            if device_id:
                _touch_device(session, device_id, request, ip)
    except Exception:  # noqa: BLE001 - 记录失败不应影响业务
        logger.warning("access log write failed", exc_info=True)


def _touch_device(session: Session, device_id: str, request: Request, ip: str) -> None:
    """登记/刷新设备，但每个设备最多 5 分钟写一次库，避免每请求一次写。"""
    now = time.monotonic()
    if now - _device_touched.get(device_id, 0.0) < _DEVICE_TOUCH_INTERVAL_S:
        return
    _device_touched[device_id] = now
    ua = (request.headers.get("user-agent") or "")[:_UA_MAX]
    label = parse_ua(ua).label
    device = session.get(Device, device_id)
    if device is None:
        session.add(
            Device(
                id=device_id,
                label=label,
                user_agent=ua,
                last_ip=ip,
                last_seen=datetime.now(timezone.utc),
            )
        )
    else:
        device.label = label
        device.user_agent = ua
        device.last_ip = ip
        device.last_seen = datetime.now(timezone.utc)
        session.add(device)
    session.commit()


def prune_access_log(session: Session, retention_days: int) -> int:
    """删除超过保留期的访问记录，返回删除条数。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = session.exec(delete(AccessLog).where(AccessLog.created_at < cutoff))
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def count_logs(session: Session) -> int:
    return int(session.exec(select(func.count()).select_from(AccessLog)).one())
