"""In-memory SSE event bus: one set of asyncio.Queue subscribers per thread."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

_subscribers: dict[str, set[asyncio.Queue[str]]] = defaultdict(set)


def subscribe(thread_id: str) -> asyncio.Queue[str]:
    queue: asyncio.Queue[str] = asyncio.Queue()
    _subscribers[thread_id].add(queue)
    return queue


def unsubscribe(thread_id: str, queue: asyncio.Queue[str]) -> None:
    queues = _subscribers.get(thread_id)
    if queues is None:
        return
    queues.discard(queue)
    if not queues:
        _subscribers.pop(thread_id, None)


def publish(thread_id: str, event: str, data: dict[str, Any]) -> None:
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    for queue in list(_subscribers.get(thread_id, ())):
        queue.put_nowait(payload)


def reset() -> None:
    """Test hook: drop all subscribers."""
    _subscribers.clear()
