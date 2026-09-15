"""存量库迁移：访问监控相关的新列（幂等）。

用法：
    python -m scripts.migrate_access_enrichment [db_path]

新表（ipgeo / device）由启动时的 SQLModel.create_all 自动创建；但**已存在的表**加字段
create_all 不会补，所以这里显式 ALTER。执行前自动备份 .bak。
以后给这些表加字段时，记得同步在这里补一条。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "app.db"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(db_path: Path, *, backup: bool = True) -> list[str]:
    db_path = Path(db_path)
    if backup:
        shutil.copy2(db_path, db_path.with_suffix(db_path.suffix + ".bak"))
    steps: list[str] = []
    conn = sqlite3.connect(db_path)
    try:
        if "device_id" not in _columns(conn, "accesslog"):
            conn.execute("ALTER TABLE accesslog ADD COLUMN device_id VARCHAR DEFAULT ''")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_accesslog_device_id ON accesslog(device_id)"
            )
            steps.append("add accesslog.device_id")
        ipgeo_columns = _columns(conn, "ipgeo")
        if ipgeo_columns:
            if "trusted" not in ipgeo_columns:
                conn.execute("ALTER TABLE ipgeo ADD COLUMN trusted BOOLEAN DEFAULT 0")
                steps.append("add ipgeo.trusted")
            if "note" not in ipgeo_columns:
                conn.execute("ALTER TABLE ipgeo ADD COLUMN note VARCHAR DEFAULT ''")
                steps.append("add ipgeo.note")
        conn.commit()
    finally:
        conn.close()
    return steps


def main() -> None:
    parser = argparse.ArgumentParser(description="accesslog.device_id 迁移")
    parser.add_argument("db_path", nargs="?", default=str(DEFAULT_DB))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    steps = migrate(Path(args.db_path), backup=not args.no_backup)
    if steps:
        for step in steps:
            print(f"✓ {step}")
    else:
        print("✓ 无需迁移（已是最新）")


if __name__ == "__main__":
    main()
