"""清理历史数据里"被反复挂载的同一份结构化结果"。

背景：旧逻辑每轮结束都会读工作区的 grading_result.json，模型没重写也照样挂到新回复上，
于是同一张批改卡片在对话里重复出现。修正后新数据不会再这样，但历史记录需要清理。

规则：按时间遍历每个会话的 assistant 消息，如果它的结果 JSON 与**上一条 assistant 消息完全相同**，
说明这一轮并没有重新生成结果 —— 清空这条消息的结果字段（内容仍保留在更早的那条上）。

用法：
    python -m scripts.cleanup_stale_results [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select  # noqa: E402

from app.db import get_engine, init_db  # noqa: E402
from app.models import Message, Thread  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="清理重复挂载的结构化结果")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()
    cleaned = 0
    with Session(get_engine()) as session:
        for thread in session.exec(select(Thread)).all():
            previous: str | None = None
            messages = session.exec(
                select(Message)
                .where(Message.thread_id == thread.id, Message.role == "assistant")
                .order_by(Message.created_at)
            ).all()
            for message in messages:
                current = message.grading_result_json
                if current is None:
                    continue
                if previous is not None and current == previous:
                    print(f"  - [{thread.title[:20]}] 清掉一条重复结果（消息 {message.id[:8]}）")
                    if not args.dry_run:
                        message.grading_result_json = None
                        message.result_type = None
                        session.add(message)
                    cleaned += 1
                else:
                    previous = current
        if not args.dry_run:
            session.commit()
    print(f"{'（dry-run，未写入）' if args.dry_run else '已清理'} {cleaned} 条重复结果")


if __name__ == "__main__":
    main()
