#!/usr/bin/env python3
"""End-to-end smoke test against the real codex app-server + DeepSeek.

Creates a user and thread directly through the backend's codex_service,
runs one pure-text turn ("1+1等于几"), streams deltas, and prints the result.

Usage (from the repo root):
    set -a; . ~/.bashrc; set +a   # DEEPSEEK_API_KEY
    backend/.venv/bin/python scripts/smoke_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlmodel import Session  # noqa: E402

from app import events  # noqa: E402
from app.codex_service.manager import CodexManager  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_engine, init_db  # noqa: E402
from app.models import Thread, User  # noqa: E402


async def main() -> int:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("error: DEEPSEEK_API_KEY is not set")
        return 2

    init_db()
    engine = get_engine()
    with Session(engine) as session:
        user = session.get(User, "smoke-user")
        if user is None:
            user = User(id="smoke-user", username="smoke", password_hash="unused")
            session.add(user)
            session.commit()
        thread = Thread(user_id=user.id, title="smoke e2e")
        session.add(thread)
        session.commit()
        thread_id = thread.id
        user_id = user.id

    print(f"user_id={user_id} thread_id={thread_id}")

    queue = events.subscribe(thread_id)
    deltas: list[str] = []

    async def drain() -> None:
        while True:
            payload = await queue.get()
            for line in payload.splitlines():
                if line.startswith("event: "):
                    print(f"\n[SSE {line.removeprefix('event: ')}]", end=" ")
                elif line.startswith("data: "):
                    data = json.loads(line.removeprefix("data: "))
                    if "delta" in data:
                        deltas.append(data["delta"])
                        print(data["delta"], end="", flush=True)
                    else:
                        print(json.dumps(data, ensure_ascii=False, indent=2))

    drain_task = asyncio.create_task(drain())
    manager = CodexManager(get_settings())
    turn_id = uuid.uuid4().hex
    try:
        await manager.run_turn(user_id, thread_id, turn_id, "1+1等于几", None)
    finally:
        await asyncio.sleep(0.3)  # let the drain print the final turn_completed event
        drain_task.cancel()
        await manager.shutdown()
        events.unsubscribe(thread_id, queue)

    print("\n--- assistant message ---")
    print("".join(deltas))

    from app.models import Message
    from sqlmodel import select

    with Session(engine) as session:
        msgs = session.exec(
            select(Message).where(Message.thread_id == thread_id)
        ).all()
    assistant = [m for m in msgs if m.role == "assistant"]
    for m in msgs:
        print(
            f"[db] role={m.role} turn_id={m.turn_id} "
            f"grading={'yes' if m.grading_result_json else 'no'}"
        )
        print(f"     content: {m.content[:200]}")
    if not assistant:
        print("SMOKE E2E FAILED: no assistant message persisted (see error event above)")
        return 1
    print("SMOKE E2E OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
