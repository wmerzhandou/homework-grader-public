"""Client for the Qwen3-ASR three-step HTTP API."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

_CHUNK_BYTES = 4 * 1024 * 1024
# ASR 服务在 GPU 忙/模型冷启动时会无响应，整会话重试
_MAX_ATTEMPTS = 3
_BACKOFF_S = (2.0, 5.0)


class AsrError(RuntimeError):
    pass


class AsrClient:
    """Implements start -> chunk* -> finish against the ASR service."""

    def __init__(
        self,
        settings: Settings | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._http = http
        self._owns_http = http is None

    async def __aenter__(self) -> "AsrClient":
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.settings.asr_base_url, timeout=120.0
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
        self._http = None

    async def transcribe_f32(self, pcm_path: Path) -> tuple[str, str]:
        """Transcribe a 16kHz mono float32-LE PCM file. Returns (language, text)."""
        assert self._http is not None, "use as an async context manager"
        last_exc: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_BACKOFF_S[min(attempt - 1, len(_BACKOFF_S) - 1)])
            try:
                session_id = await self._start()
                with pcm_path.open("rb") as fh:
                    while chunk := fh.read(_CHUNK_BYTES):
                        await self._send_chunk(session_id, chunk)
                return await self._finish(session_id)
            except (httpx.HTTPError, AsrError) as exc:
                last_exc = exc
                logger.warning("ASR attempt %d/%d failed: %s", attempt + 1, _MAX_ATTEMPTS, exc)
        raise AsrError(f"ASR failed after {_MAX_ATTEMPTS} attempts: {last_exc}")

    async def _start(self) -> str:
        resp = await self._http.post("/api/start")
        resp.raise_for_status()
        data = resp.json()
        session_id = data.get("session_id")
        if not session_id:
            raise AsrError(f"ASR /api/start returned no session_id: {data!r}")
        return session_id

    async def _send_chunk(self, session_id: str, chunk: bytes) -> None:
        resp = await self._http.post(
            "/api/chunk",
            params={"session_id": session_id},
            content=chunk,
            headers={"Content-Type": "application/octet-stream"},
        )
        resp.raise_for_status()

    async def _finish(self, session_id: str) -> tuple[str, str]:
        resp = await self._http.post("/api/finish", params={"session_id": session_id})
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("language", "")), str(data.get("text", ""))
