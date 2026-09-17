"""存量库迁移：thread.task_type 与 message.result_type。

用法：
    python -m scripts.migrate_task_type_result_type [db_path]

默认操作 ../data/app.db（仓库根下的真实库），执行前自动备份为 <db>.bak。
幂等：列已存在时跳过对应 ALTER TABLE。
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "app.db"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(db_path: Path, *, backup: bool = True) -> None:
    db_path = Path(db_path)
    if backup:
        shutil.copy2(db_path, db_path.with_suffix(db_path.suffix + ".bak"))
    conn = sqlite3.connect(db_path)
    try:
        if "task_type" not in _columns(conn, "thread"):
            conn.execute(
                "ALTER TABLE thread ADD COLUMN task_type VARCHAR NOT NULL DEFAULT 'auto'"
            )
        if "result_type" not in _columns(conn, "message"):
            conn.execute("ALTER TABLE message ADD COLUMN result_type VARCHAR")
        # 存量已有批改结果的消息回填 result_type
        conn.execute(
            "UPDATE message SET result_type = 'grading' "
            "WHERE grading_result_json IS NOT NULL AND result_type IS NULL"
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    migrate(target)
    print(f"migrated {target}")
