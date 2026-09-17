"""为历史数据补齐"跨会话引用"所需的两样东西：

1. 每个会话的**会话码**（thread.code，未分配过才补）；
2. 每份材料的**可读文本**（`.derived/<material_id>.md|.txt`）：
   - 文档：用 markitdown 转成全文 markdown；
   - 音频/视频：把库里已有的 ASR 转写写入 txt；
   - 图片：不需要（原图本身就是可读形式）。

用法：
    python -m scripts.backfill_reference_data [--dry-run]

幂等：已有会话码/可读文本的不会重复处理。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select  # noqa: E402

from app.codex_service import workspace  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_engine, init_db  # noqa: E402
from app.models import Material, MaterialKind, Thread  # noqa: E402
from app.pipeline import docs  # noqa: E402
from app.pipeline.router import _write_derived  # noqa: E402
from app.thread_codes import ensure_code  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="补齐会话码与可读文本")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    init_db()
    codes_added = 0
    texts_added = 0
    skipped: list[str] = []

    with Session(get_engine()) as session:
        threads = list(session.exec(select(Thread)).all())
        for thread in threads:
            if thread.code:
                continue
            code = ensure_code(session, thread)
            print(f"  + 会话码 {code} → 《{thread.title[:20]}》")
            codes_added += 1
            if not args.dry_run:
                session.add(thread)
        if not args.dry_run and codes_added:
            session.commit()

        for material in session.exec(select(Material)).all():
            if material.kind == MaterialKind.image or material.text_path:
                continue
            thread = session.get(Thread, material.thread_id)
            if thread is None or not material.stored_path:
                continue
            path = Path(material.stored_path)
            if not path.is_file():
                skipped.append(f"{material.filename}（原文件不在）")
                continue
            try:
                if material.kind == MaterialKind.document:
                    text = docs._convert_text(path)  # markitdown 全文
                    suffix = ".md"
                elif material.kind in (MaterialKind.audio, MaterialKind.video):
                    text = material.transcript or ""
                    suffix = ".txt"
                    if not text:
                        skipped.append(f"{material.filename}（没有转写）")
                        continue
                else:
                    continue
            except Exception as exc:  # noqa: BLE001
                skipped.append(f"{material.filename}（转换失败：{exc}）")
                continue
            print(f"  + 可读文本 {material.filename} ({len(text)} 字)")
            texts_added += 1
            if not args.dry_run:
                material.text_path = _write_derived(
                    settings, thread.user_id, thread.id, material.id, suffix, text
                )
                session.add(material)
        if not args.dry_run and texts_added:
            session.commit()

    prefix = "（dry-run，未写入）" if args.dry_run else "已补齐"
    print(f"{prefix}：会话码 {codes_added} 个，可读文本 {texts_added} 份")
    for item in skipped:
        print(f"  ! 跳过 {item}")


if __name__ == "__main__":
    main()
