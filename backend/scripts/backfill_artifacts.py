"""把历史会话里"模型已产出但没被登记"的文件补登为 Artifact。

用法：
    python -m scripts.backfill_artifacts [--dry-run]

规则与运行时一致：跳过用户上传的原文件、keyframes/、grading_result.json / english_result.json、
隐藏目录（.git/.codex/.agents 等）。补登的产出物挂到该会话**最后一条 assistant 消息**上
（历史数据无法精确知道是哪一轮产生的，这是最合理的归属）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select  # noqa: E402

from app.artifacts import is_ignored  # noqa: E402
from app.codex_service import workspace  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_engine, init_db  # noqa: E402
from app.models import Artifact, Material, Message, Thread  # noqa: E402
from app.pipeline.images import detect_kind  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="补登历史产出物")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--prepare-previews",
        action="store_true",
        help="顺便为已有图片/视频生成浏览器友好的预览副本（缩放/转码）",
    )
    args = parser.parse_args()

    settings = get_settings()
    init_db()
    created = 0
    with Session(get_engine()) as session:
        known = {a.stored_path for a in session.exec(select(Artifact)).all()}
        uploaded = {m.stored_path for m in session.exec(select(Material)).all() if m.stored_path}
        for thread in session.exec(select(Thread)).all():
            ws = workspace.workspace_dir(settings, thread.user_id, thread.id)
            if not ws.is_dir():
                continue
            last_assistant = session.exec(
                select(Message)
                .where(Message.thread_id == thread.id, Message.role == "assistant")
                .order_by(Message.created_at.desc())
            ).first()
            for path in sorted(ws.rglob("*")):
                if not path.is_file() or is_ignored(path.relative_to(ws)):
                    continue
                if str(path) in known or str(path) in uploaded:
                    continue
                stat = path.stat()
                print(f"  + [{thread.title[:18]}] {path.name} ({stat.st_size // 1024} KB)")
                if args.dry_run:
                    continue
                session.add(
                    Artifact(
                        thread_id=thread.id,
                        message_id=last_assistant.id if last_assistant else None,
                        filename=path.name,
                        stored_path=str(path),
                        kind=detect_kind(path.name).value,
                        size=stat.st_size,
                    )
                )
                created += 1
        if not args.dry_run:
            session.commit()
    if args.prepare_previews and not args.dry_run:
        import asyncio

        from app import media_prep

        with Session(get_engine()) as session:
            pending = [
                a.id
                for a in session.exec(select(Artifact)).all()
                if a.kind in ("image", "video") and not a.preview_path
            ]
        if pending:
            print(f"为 {len(pending)} 个产出物生成预览副本…")
            asyncio.run(_prepare_all(pending))
    print(f"{'（dry-run，未写入）' if args.dry_run else '已补登'} {created} 个产出物")


async def _prepare_all(ids: list[str]) -> None:
    from app import media_prep

    for artifact_id in ids:
        await media_prep.prepare_artifact(artifact_id)


if __name__ == "__main__":
    main()
