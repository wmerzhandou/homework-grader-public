"""SSE endpoint: realtime per-thread event stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session

from .. import events
from ..db import get_engine
from ..models import AuthToken, Thread

router = APIRouter(tags=["events"])

_HEARTBEAT_S = 25.0


def _authorize_thread(thread_id: str, authorization: str | None) -> None:
    """Validate bearer token and thread ownership with a short-lived session.

    SSE 是长连接。Depends(get_session) 这类 yield 依赖要等流式响应结束才清理，
    会在整个连接生命周期内占住数据库连接池，几次重连就会把池耗光，所以不能走 Depends。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token"
        )
    token = authorization.removeprefix("Bearer ").strip()
    with Session(get_engine()) as session:
        record = session.get(AuthToken, token)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
            )
        thread = session.get(Thread, thread_id)
        if thread is None or thread.user_id != record.user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="thread not found"
            )


@router.get("/api/threads/{thread_id}/events")
async def thread_events(
    thread_id: str,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    _authorize_thread(thread_id, authorization)

    queue = events.subscribe(thread_id)

    async def stream() -> AsyncIterator[str]:
        try:
            yield ": connected\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield payload
        finally:
            events.unsubscribe(thread_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
