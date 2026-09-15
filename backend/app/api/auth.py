"""Auth endpoints: register (invite-gated), login, me, logout, media ticket."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    generate_invite_code,
    get_current_user,
    hash_password,
    issue_token,
    revoke_token,
    user_by_username,
    verify_password,
)
from ..config import get_settings
from ..db import get_session
from ..models import InviteCode, User
from ..security import (
    SlidingWindowLimiter,
    client_ip,
    ensure_app_secret,
    issue_media_ticket,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

# 登录/注册限流：挡在线口令爆破与邀请码枚举。进程内实现，单进程部署够用。
_auth_limiters: dict[tuple[int, float], SlidingWindowLimiter] = {}


def _limiter() -> SlidingWindowLimiter:
    settings = get_settings()
    key = (settings.auth_rate_limit_attempts, settings.auth_rate_limit_window_s)
    limiter = _auth_limiters.get(key)
    if limiter is None:
        limiter = SlidingWindowLimiter(limit=key[0], window_s=key[1])
        _auth_limiters[key] = limiter
    return limiter


def reset_rate_limits() -> None:
    """测试钩子。"""
    for limiter in _auth_limiters.values():
        limiter.reset()


def _rate_limit_or_429(request: Request, key: str) -> None:
    allowed, retry_after = _limiter().check(f"{key}:{client_ip(request)}")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="尝试过于频繁，请稍后再试",
            headers={"Retry-After": str(retry_after)},
        )


def _validate_credentials(username: str, password: str) -> None:
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名需为 3-32 位字母、数字、下划线、点或连字符",
        )
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"密码长度需在 {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} 位之间",
        )


class AuthRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(AuthRequest):
    invite_code: str


class UserOut(BaseModel):
    id: str
    username: str


class AuthResponse(BaseModel):
    token: str
    user: UserOut


def _auth_response(session: Session, user: User) -> AuthResponse:
    return AuthResponse(
        token=issue_token(session, user), user=UserOut(id=user.id, username=user.username)
    )


@router.post("/register", response_model=AuthResponse)
def register(
    body: RegisterRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> AuthResponse:
    _rate_limit_or_429(request, "register")
    username = body.username.strip()
    _validate_credentials(username, body.password)
    invite = session.get(InviteCode, body.invite_code)
    if invite is None or invite.used_by is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid invite code")
    if user_by_username(session, username) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username taken")
    user = User(username=username, password_hash=hash_password(body.password))
    session.add(user)
    session.commit()
    invite.used_by = user.id
    session.add(invite)
    session.commit()
    return _auth_response(session, user)


@router.post("/login", response_model=AuthResponse)
def login(
    body: AuthRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> AuthResponse:
    _rate_limit_or_429(request, "login")
    user = user_by_username(session, body.username.strip())
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )
    # 登录成功清零计数，避免正常用户被自己之前的输错攒到限流
    _limiter().reset(f"login:{client_ip(request)}")
    return _auth_response(session, user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(id=user.id, username=user.username)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> None:
    """吊销当前 token（真正的服务端登出）。"""
    if authorization and authorization.startswith("Bearer "):
        revoke_token(session, authorization.removeprefix("Bearer ").strip())


class MediaTicketOut(BaseModel):
    ticket: str
    expires_in: int


@router.post("/media-ticket", response_model=MediaTicketOut)
def media_ticket(user: User = Depends(get_current_user)) -> MediaTicketOut:
    """签发短时下载票据。

    浏览器 <img>/<audio>/<video> 无法携带 Authorization 头，前端据此拼 URL；
    票据只对材料下载接口有效、有效期短、绑定当前用户，避免长期 token 出现在 URL/日志里。
    """
    settings = get_settings()
    secret = ensure_app_secret(settings.data_dir, settings.app_secret)
    ttl = settings.media_ticket_ttl_s
    return MediaTicketOut(
        ticket=issue_media_ticket(secret, user.id, ttl), expires_in=ttl
    )


def seed_admin(session: Session, admin_password: str) -> list[str]:
    """Create the admin user on first boot and top up invite codes when none are left.

    只在"没有未使用邀请码"时补 3 个，避免每次重启都堆码；
    新码写入 data/invite_codes.txt(600)，不写进日志。
    """
    codes: list[str] = []
    if user_by_username(session, "admin") is None:
        session.add(
            User(username="admin", password_hash=hash_password(admin_password), is_admin=True)
        )
        session.commit()
    unused = session.exec(select(InviteCode).where(InviteCode.used_by.is_(None))).all()
    if not unused:
        for _ in range(3):
            code = generate_invite_code()
            session.add(InviteCode(code=code))
            codes.append(code)
        session.commit()
    return codes
