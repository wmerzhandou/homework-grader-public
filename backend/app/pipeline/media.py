"""ffmpeg/ffprobe helpers for audio/video preprocessing."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import Settings, get_settings


async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"command failed ({proc.returncode}): {cmd[0]} ... {tail}")


async def to_f32_pcm(src: Path, dst: Path, settings: Settings | None = None) -> Path:
    """Transcode any audio/video container to 16kHz mono float32-LE raw PCM."""
    settings = settings or get_settings()
    await _run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-i",
            str(src),
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(dst),
        ]
    )
    return dst


async def has_video_stream(src: Path, settings: Settings | None = None) -> bool:
    """Probe whether the file contains a video stream.

    MediaRecorder audio recordings also use the .webm container, so the
    extension alone cannot tell audio-only files from real videos.
    """
    settings = settings or get_settings()
    ffprobe = settings.ffmpeg_bin.replace("ffmpeg", "ffprobe")
    proc = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(src),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode == 0 and stdout.decode().strip() == "video"


async def probe_duration_s(src: Path, settings: Settings | None = None) -> float | None:
    settings = settings or get_settings()
    ffprobe = settings.ffmpeg_bin.replace("ffmpeg", "ffprobe")
    proc = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(src),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return None


async def extract_keyframes(
    video: Path, out_dir: Path, prefix: str, settings: Settings | None = None
) -> list[Path]:
    """Extract three keyframes (start / middle / end) as JPEG files."""
    settings = settings or get_settings()
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = await probe_duration_s(video, settings)
    if duration is None or duration <= 0:
        offsets = [0.0]
    else:
        offsets = [0.0, duration / 2, max(duration - 0.1, duration / 2)]
    frames: list[Path] = []
    for idx, offset in enumerate(offsets):
        target = out_dir / f"{prefix}_{idx}.jpg"
        await _run(
            [
                settings.ffmpeg_bin,
                "-y",
                "-ss",
                f"{offset:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                # 限制帧尺寸，避免图片输入超出模型 API 请求体上限
                "-vf",
                "scale='min(1600,iw)':-1",
                "-q:v",
                "3",
                str(target),
            ]
        )
        if target.exists():
            frames.append(target)
    return frames
