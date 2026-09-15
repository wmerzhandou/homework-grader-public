"""Password hashing, token issuance, and auth dependencies."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session, select

from .config import get_settings
from .db import get_session
from .models import AuthToken, User

_PBKDF2_ITERATIONS = 200_000
# 口令长度上限：避免超长口令让 PBKDF2 每次校验做无谓的大量工作（DoS）
MAX_PASSWORD_LENGTH = 128
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iterations, salt, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def issue_token(session: Session, user: User, ttl_s: int | None = None) -> str:
    """签发登录 token，带过期时间。ttl_s 为 None 时取 settings.auth_token_ttl_s。"""
    ttl = get_settings().auth_token_ttl_s if ttl_s is None else ttl_s
    token = secrets.token_hex(32)
    session.add(
        AuthToken(
            token=token,
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl) if ttl > 0 else None,
        )
    )
    session.commit()
    return token


def generate_invite_code() -> str:
    return secrets.token_hex(4).upper()


def token_is_expired(record: AuthToken) -> bool:
    """expires_at 为 NULL 的历史 token 视为未过期（由迁移脚本负责回填）。"""
    expires_at = record.expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def get_current_user(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token"
        )
    token = authorization.removeprefix("Bearer ").strip()
    user = _user_from_token_record(session, token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    return user


def _user_from_token_record(session: Session, token: str) -> User | None:
    """校验 token 是否有效（存在且未过期），返回对应用户。"""
    record = session.get(AuthToken, token)
    if record is None or token_is_expired(record):
        return None
    return session.get(User, record.user_id)


def user_by_username(session: Session, username: str) -> User | None:
    return session.exec(select(User).where(User.username == username)).first()


def user_from_token(session: Session, token: str) -> User | None:
    """Resolve a bearer token string to a user (or None)."""
    return _user_from_token_record(session, token)


def revoke_token(session: Session, token: str) -> bool:
    """吊销单个 token（登出）。返回是否确实删掉了记录。"""
    record = session.get(AuthToken, token)
    if record is None:
        return False
    session.delete(record)
    session.commit()
    return True
