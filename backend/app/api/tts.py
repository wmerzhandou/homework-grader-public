"""TTS 代理端点：文本 → IndexTTS（经本机 tts-story 应用）→ WAV 音频。

按文本哈希做磁盘缓存，同一段讲解只合成一次。
"""

from __future__ import annotations

import base64
import hashlib
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel

from ..auth import get_current_user
from ..config import get_settings
from ..models import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tts"])

_MAX_TEXT_CHARS = 2000
_TTS_TIMEOUT_S = 180.0


class TtsRequest(BaseModel):
    text: str


@router.post("/api/tts")
async def synthesize(
    body: TtsRequest, user: User = Depends(get_current_user)
) -> Response:
    text = body.text.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="text must not be empty"
        )
    if len(text) > _MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"text too long (max {_MAX_TEXT_CHARS} chars)",
        )

    settings = get_settings()
    cache_dir = settings.data_dir / "tts_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cached = cache_dir / f"{cache_key}.wav"
    if cached.exists():
        return Response(content=cached.read_bytes(), media_type="audio/wav")

    try:
        async with httpx.AsyncClient(timeout=_TTS_TIMEOUT_S) as http:
            resp = await http.post(
                f"{settings.tts_base_url}/api/preview",
                json={"text": text, "tts_engine": "index_tts", "voice": "default"},
            )
    except httpx.HTTPError as exc:
        logger.warning("TTS service unreachable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="语音合成服务不可用"
        ) from exc

    if resp.status_code != status.HTTP_200_OK:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="语音合成失败"
        )
    data = resp.json()
    if not data.get("success") or not data.get("audio_base64"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(data.get("error") or "语音合成失败"),
        )

    audio = base64.b64decode(data["audio_base64"])
    cached.write_bytes(audio)
    return Response(content=audio, media_type="audio/wav")
