"""重置某个账号的登录口令（保留 user_id、会话、材料与 token）。

用法：
    python -m scripts.set_password <username> <new_password> [db_path]

口令永远不会被打印或写进日志；改完直接生效，已登录设备不受影响。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import (  # noqa: E402
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
)

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "app.db"


def set_password(username: str, password: str, db_path: Path) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise SystemExit(
            f"口令长度需在 {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} 位之间"
        )
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM user WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"用户不存在：{username}")
        conn.execute(
            "UPDATE user SET password_hash = ? WHERE id = ?", (hash_password(password), row[0])
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="重置账号口令")
    parser.add_argument("username")
    parser.add_argument("password")
    parser.add_argument("db_path", nargs="?", default=str(DEFAULT_DB))
    args = parser.parse_args()
    set_password(args.username, args.password, Path(args.db_path))
    print(f"✓ 已更新 {args.username} 的口令（user_id 与会话数据保持不变）")


if __name__ == "__main__":
    main()
