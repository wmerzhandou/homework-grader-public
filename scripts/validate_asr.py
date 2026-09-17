#!/usr/bin/env python3
"""Validate the Qwen3-ASR three-step API end to end.

Usage:
    python3 scripts/validate_asr.py <audio-or-video-file> [asr_base_url]

Transcodes the input to 16kHz mono float32-LE PCM with ffmpeg, then runs
start -> chunk -> finish against the ASR service and prints the transcript.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

DEFAULT_ASR_URL = "http://127.0.0.1:8020"
CHUNK_BYTES = 4 * 1024 * 1024


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    base_url = (sys.argv[2] if len(sys.argv) > 2 else DEFAULT_ASR_URL).rstrip("/")
    if not src.is_file():
        print(f"error: no such file: {src}")
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        pcm = Path(tmp) / "audio.f32"
        print(f"[1/4] ffmpeg transcode -> 16kHz mono f32le ...")
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-f", "f32le", "-acodec", "pcm_f32le", "-ar", "16000", "-ac", "1",
                str(pcm),
            ],
            check=True,
            capture_output=True,
        )
        size = pcm.stat().st_size
        print(f"      pcm bytes: {size}")

        with httpx.Client(base_url=base_url, timeout=120.0) as client:
            print(f"[2/4] POST /api/start ...")
            resp = client.post("/api/start")
            resp.raise_for_status()
            session_id = resp.json()["session_id"]
            print(f"      session_id: {session_id}")

            print(f"[3/4] POST /api/chunk ...")
            with pcm.open("rb") as fh:
                idx = 0
                while True:
                    chunk = fh.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    resp = client.post(
                        "/api/chunk",
                        params={"session_id": session_id},
                        content=chunk,
                        headers={"Content-Type": "application/octet-stream"},
                    )
                    resp.raise_for_status()
                    partial = resp.json()
                    print(f"      chunk {idx}: {partial.get('text', '')[:80]}")
                    idx += 1

            print(f"[4/4] POST /api/finish ...")
            resp = client.post("/api/finish", params={"session_id": session_id})
            resp.raise_for_status()
            result = resp.json()

    print(f"\nlanguage: {result.get('language')}")
    print(f"text:     {result.get('text')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
