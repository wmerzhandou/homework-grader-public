"""存量库迁移：authtoken.expires_at（登录 token 过期时间）。

用法：
    python -m scripts.migrate_security_hardening [db_path] [--ttl-days 30]

默认操作 ../data/app.db（仓库根下的真实库），执行前自动备份为 <db>.bak。
幂等：列已存在时跳过 ALTER TABLE；只回填 expires_at 为 NULL 的历史 token
（回填成 created_at + TTL，而不是立即失效，避免把所有已登录用户踢下线）。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "app.db"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(db_path: Path, *, backup: bool = True, ttl_days: int = 30) -> list[str]:
    """返回执行过的迁移步骤名列表。"""
    db_path = Path(db_path)
    if backup:
        shutil.copy2(db_path, db_path.with_suffix(db_path.suffix + ".bak"))
    steps: list[str] = []
    conn = sqlite3.connect(db_path)
    try:
        if "expires_at" not in _columns(conn, "authtoken"):
            conn.execute("ALTER TABLE authtoken ADD COLUMN expires_at DATETIME")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_authtoken_expires_at ON authtoken(expires_at)")
            steps.append("add authtoken.expires_at")

        # 回填：历史 token 以 created_at + TTL 作为过期时间
        legacy = conn.execute(
            "SELECT token, created_at FROM authtoken WHERE expires_at IS NULL"
        ).fetchall()
        for token, created_at in legacy:
            try:
                created = datetime.fromisoformat(str(created_at))
            except ValueError:
                created = datetime.utcnow()
            expires = created + timedelta(days=ttl_days)
            conn.execute(
                "UPDATE authtoken SET expires_at = ? WHERE token = ?",
                (expires.isoformat(sep=" "), token),
            )
        if legacy:
            steps.append(f"backfill expires_at for {len(legacy)} legacy token(s)")
        conn.commit()
    finally:
        conn.close()
    return steps


def main() -> None:
    parser = argparse.ArgumentParser(description="authtoken.expires_at 迁移")
    parser.add_argument("db_path", nargs="?", default=str(DEFAULT_DB))
    parser.add_argument("--ttl-days", type=int, default=30)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    steps = migrate(
        Path(args.db_path), backup=not args.no_backup, ttl_days=args.ttl_days
    )
    if steps:
        for step in steps:
            print(f"✓ {step}")
    else:
        print("✓ 无需迁移（已是最新）")


if __name__ == "__main__":
    main()
