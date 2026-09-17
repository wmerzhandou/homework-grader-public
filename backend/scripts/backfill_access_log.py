"""把 uvicorn 的历史访问日志回填进 accesslog 表（监控页的数据来源）。

用法（journal 需要 root 读取，所以由 sudo 输出 JSON 再管道进来）：

    sudo journalctl -u homework-grader -o json --no-pager \
      | backend/.venv/bin/python -m scripts.backfill_access_log

规则与运行时中间件保持一致：跳过本机回环、静态资源、健康检查、管理页；查询串不入库。
解析不到的日志行会被忽略（脚本只负责"尽力回填"，不追求 100% 完整）。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.access import should_record  # noqa: E402
from app.db import Session, get_engine, init_db  # noqa: E402
from app.models import AccessLog  # noqa: E402

# MESSAGE 形如：INFO:     144.0.143.74:55514 - "GET /api/threads HTTP/1.1" 200 OK
# 前面有日志级别前缀，所以用 search 而不是 match。
_LINE = re.compile(
    r'(?P<ip>[0-9a-fA-F:.]+):\d+ - "(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[\d.]+" (?P<status>\d{3})'
)


def parse(stream) -> list[AccessLog]:
    rows: list[AccessLog] = []
    for raw in stream:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        message = entry.get("MESSAGE", "")
        if " - \"" not in message:
            continue
        match = _LINE.search(message.strip())
        if not match:
            continue
        path = match.group("path").split("?", 1)[0]
        ip = match.group("ip")
        if not should_record(ip, path):
            continue
        ts = entry.get("__REALTIME_TIMESTAMP")
        created_at = (
            datetime.fromtimestamp(int(ts) / 1_000_000, tz=timezone.utc)
            if ts
            else datetime.now(timezone.utc)
        )
        rows.append(
            AccessLog(
                ip=ip,
                method=match.group("method"),
                path=path,
                status=int(match.group("status")),
                user_agent="",  # uvicorn 默认不记录 UA
                created_at=created_at,
            )
        )
    return rows


def main() -> None:
    init_db()
    rows = parse(sys.stdin)
    if not rows:
        print("没有可回填的记录")
        return
    # 先统计好再写库：commit 会让 ORM 对象过期，之后再读属性会 detached
    summary = Counter(r.ip for r in rows)
    with Session(get_engine()) as session:
        session.add_all(rows)
        session.commit()
    print(f"已回填 {len(rows)} 条访问记录，涉及 {len(summary)} 个 IP")
    for ip, count in summary.most_common():
        print(f"  {ip}: {count} 条")


if __name__ == "__main__":
    main()
