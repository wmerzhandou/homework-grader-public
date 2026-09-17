"""安全加固基础设施：响应头、限流、短时下载票据、访问日志脱敏、票据签名密钥。

这个模块只依赖 stdlib 与 starlette，方便被 main / api 层复用，也方便单测。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 响应头
# --------------------------------------------------------------------------
# 前端是 Vite 打包的同源 SPA：脚本/样式/字体都是本地文件，
# 图片与音视频走同源接口（TTS 播放用 blob:），因此可以收紧到 self。
# style-src 保留 'unsafe-inline'：React 的 style 属性在部分浏览器里按内联样式处理。
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

# 摄像头/麦克风要给自己用（作业拍照、录音），其余能力默认关闭。
PERMISSIONS_POLICY = "camera=(self), microphone=(self), geolocation=(), payment=()"

_SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": PERMISSIONS_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin",
}

_HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """给所有响应补安全头；HTTPS 下额外加 HSTS。"""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            # 已有显式设置（例如 FileResponse 的 Content-Disposition）不覆盖
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/api/"):
            # API 响应（含带短时票据的材料下载）禁止中间缓存落盘
            response.headers.setdefault("Cache-Control", "no-store")
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", _HSTS)
        return response


# --------------------------------------------------------------------------
# 限流（进程内滑动窗口）
# --------------------------------------------------------------------------
class SlidingWindowLimiter:
    """按 key 计数的滑动窗口限流器（单进程内存实现）。

    服务是单进程 uvicorn + NullPool，进程内限流足够挡住在线爆破；
    多实例部署时需要换成 Redis 之类的集中式存储。
    """

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """返回 (是否放行, 若被拒还需等待的秒数)。"""
        now = time.monotonic()
        cutoff = now - self.window_s
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry_after = max(1, int(hits[0] + self.window_s - now) + 1)
                return False, retry_after
            hits.append(now)
        return True, 0

    def reset(self, key: str | None = None) -> None:
        """测试钩子：清空某个 key 或全部计数。"""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


def client_ip(request: Request) -> str:
    """取客户端 IP。

    后端只监听本机/经 vite 代理，不信任任意 X-Forwarded-For，
    只取直连地址，避免伪造头绕过限流。
    """
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------
# 短时下载票据（HMAC 签名，替代把长期 token 放进 URL）
# --------------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64url(digest)


def issue_media_ticket(secret: str, user_id: str, ttl_s: int, now: float | None = None) -> str:
    """签发 <user_id>.<exp>.<sig> 形式的短时票据。"""
    expires_at = int(now if now is not None else time.time()) + ttl_s
    payload = f"{user_id}.{expires_at}"
    return f"{payload}.{_sign(secret, payload)}"


def verify_media_ticket(secret: str, ticket: str, now: float | None = None) -> str | None:
    """校验票据，返回 user_id；签名不符或过期返回 None。"""
    parts = ticket.split(".")
    if len(parts) != 3:
        return None
    user_id, expires_raw, signature = parts
    if not user_id or not expires_raw.isdigit():
        return None
    expected = _sign(secret, f"{user_id}.{expires_raw}")
    if not hmac.compare_digest(expected, signature):
        return None
    if int(expires_raw) < int(now if now is not None else time.time()):
        return None
    return user_id


_secret_cache: dict[str, str] = {}
_secret_lock = threading.Lock()


def ensure_app_secret(data_dir: Path, explicit: str | None = None) -> str:
    """取票据签名密钥：优先显式配置，否则在 data_dir 下落盘保存并复用。

    落盘文件权限 600，保证重启后既有票据仍然有效。
    """
    if explicit:
        return explicit
    key = str(Path(data_dir))
    with _secret_lock:
        cached = _secret_cache.get(key)
        if cached:
            return cached
        path = Path(data_dir) / ".app_secret"
        secret = ""
        if path.exists():
            secret = path.read_text(encoding="utf-8").strip()
        if not secret:
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            secret = secrets.token_hex(32)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(secret, encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        _secret_cache[key] = secret
        return secret


# --------------------------------------------------------------------------
# 访问日志脱敏
# --------------------------------------------------------------------------
class StripQueryStringFilter(logging.Filter):
    """把 uvicorn.access 记录里的查询串删掉。

    uvicorn 默认记录完整 request line，`?ticket=`/`?token=` 这类凭据会进 journald；
    AccessFormatter 的 args 固定为 (client_addr, method, full_path, http_version, status)。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path = args[2].split("?", 1)[0]
            if path != args[2]:
                record.args = (args[0], args[1], path, args[3], args[4])
        return True


def harden_access_log() -> None:
    """给 uvicorn.access logger 装上查询串过滤器（幂等）。"""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, StripQueryStringFilter) for f in access_logger.filters):
        access_logger.addFilter(StripQueryStringFilter())


# --------------------------------------------------------------------------
# 文件权限
# --------------------------------------------------------------------------
def restrict_path(path: Path, mode: int, *, directory: bool = False) -> None:
    """尽力收紧权限；失败只记日志（例如文件系统不支持）。"""
    try:
        path.chmod(mode)
    except OSError as exc:  # pragma: no cover - 依赖运行环境
        logger.warning("could not chmod %s to %o: %s", path, mode, exc)


def harden_data_dir(data_dir: Path, db_path: Path | None = None) -> None:
    """data/ 750、app.db 与附属文件 600。"""
    if data_dir.exists():
        restrict_path(data_dir, 0o750, directory=True)
    if db_path is not None and db_path.exists():
        restrict_path(db_path, 0o600)
        for suffix in ("-wal", "-shm"):
            side = db_path.with_name(db_path.name + suffix)
            if side.exists():
                restrict_path(side, 0o600)
