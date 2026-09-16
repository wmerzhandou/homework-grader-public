"""模型发起的"生成任务"：语音合成（IndexTTS）与图片生成（mmx CLI）。

为什么用"请求文件"而不是让模型直接调服务：
codex 的 shell 跑在沙箱里、**没有网络**（连 127.0.0.1 的服务也连不上），所以模型无法自己调用
本机 TTS 或 mmx CLI。约定：模型在会话工作区根目录写一个请求 JSON，后端在这一轮结束时执行、
把产物写回工作区，随后产出物链路自动把它送到聊天窗口和详情页。

请求文件（都是隐藏文件，不会被登记成产出物）：
- `.tts_request.json`   {"text": "...", "filename": "朗读.wav", "voice": "default", "speed": 1.0}
- `.image_request.json` {"prompt": "...", "filename": "插图.jpg", "aspect_ratio": "1:1", "n": 1}

执行失败不会影响主流程：请求文件会被删掉，并向用户回一条明确说明。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import Settings
from . import video_gen

logger = logging.getLogger(__name__)

TTS_REQUEST_FILE = ".tts_request.json"
IMAGE_REQUEST_FILE = ".image_request.json"
VIDEO_REQUEST_FILE = ".video_request.json"

_TTS_MAX_CHARS = 2000
_IMAGE_MAX_PROMPT = 800
_IMAGE_MAX_COUNT = 4
_VIDEO_MAX_PROMPT = 1500
_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{2,40}$")
_SAFE_NAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


@dataclass
class GenerationResult:
    files: list[Path] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


def _safe_name(raw: str | None, default: str, suffix: str) -> str:
    name = (raw or "").strip() or default
    name = _SAFE_NAME_RE.sub("_", name).lstrip(".") or default
    if not name.lower().endswith(suffix):
        name = f"{Path(name).stem}{suffix}"
    return name


def _sniff_suffix(path: Path) -> str | None:
    """按魔数判断真实图片格式，避免"文件内容是 JPEG 但扩展名是 .png"这种不一致。"""
    try:
        head = path.read_bytes()[:12]
    except OSError:
        return None
    if head.startswith(b"\xff\xd8"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    return None


async def _synthesize_speech(
    *, workspace: Path, settings: Settings, request: dict, notices: list[str]
) -> list[Path]:
    text = str(request.get("text") or "").strip()
    if not text:
        notices.append("⚠️ 语音生成失败：`.tts_request.json` 里缺少 text。")
        return []
    if len(text) > _TTS_MAX_CHARS:
        text = text[:_TTS_MAX_CHARS]
        notices.append(f"⚠️ 语音文本过长，只朗读了前 {_TTS_MAX_CHARS} 字。")
    engine = str(request.get("engine") or "index_tts").strip().lower()
    voice = str(request.get("voice") or "default").strip() or "default"
    try:
        speed = float(request.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = min(max(speed, 0.5), 2.0)
    emotion = str(request.get("emotion") or "").strip()

    # 引擎二选一：
    #   index_tts（默认）= 本机 IndexTTS-2.5，音色由服务器配置决定，质量稳
    #   mmx            = MiniMax 云端音色库，可选音色/情感/语速（几十个中文音色）
    if engine in ("mmx", "minimax"):
        return await _synthesize_speech_mmx(
            workspace=workspace,
            settings=settings,
            text=text,
            voice=voice,
            speed=speed,
            emotion=emotion,
            request=request,
            notices=notices,
        )

    target = workspace / _safe_name(request.get("filename"), "朗读.wav", ".wav")

    cache_dir = settings.data_dir / "tts_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(f"{text}|{voice}|{speed}".encode()).hexdigest()
    cached = cache_dir / f"{cache_key}.wav"

    audio: bytes | None = None
    if cached.is_file():
        audio = cached.read_bytes()
    else:
        try:
            async with httpx.AsyncClient(timeout=settings.tts_generation_timeout_s) as http:
                resp = await http.post(
                    f"{settings.tts_base_url}/api/preview",
                    json={
                        "text": text,
                        "tts_engine": "index_tts",
                        "voice": voice,
                        "speed": speed,
                    },
                )
            if resp.status_code != 200:
                notices.append(f"⚠️ 语音生成失败：TTS 服务返回 {resp.status_code}。")
                return []
            data = resp.json()
            if not data.get("success") or not data.get("audio_base64"):
                notices.append(f"⚠️ 语音生成失败：{data.get('error') or '服务未返回音频'}。")
                return []
            audio = base64.b64decode(data["audio_base64"])
            cached.write_bytes(audio)
        except httpx.HTTPError as exc:
            logger.warning("TTS 服务不可用: %s", exc)
            notices.append("⚠️ 语音生成失败：本机语音合成服务不可用。")
            return []
        except Exception:  # noqa: BLE001
            logger.warning("语音生成异常", exc_info=True)
            notices.append("⚠️ 语音生成失败：内部错误。")
            return []

    target.write_bytes(audio)
    return [target]


async def _synthesize_speech_mmx(
    *,
    workspace: Path,
    settings: Settings,
    text: str,
    voice: str,
    speed: float,
    emotion: str,
    request: dict,
    notices: list[str],
) -> list[Path]:
    """用 mmx CLI 合成语音（可选音色/情感），产物仍是工作区里的音频文件。"""
    mmx = settings.mmx_bin
    if not mmx or not Path(mmx).exists():
        notices.append("⚠️ 语音生成失败：服务器上找不到 mmx CLI。")
        return []
    if not _VOICE_ID_RE.fullmatch(voice):
        notices.append(f"⚠️ 语音生成失败：音色名不合法（{voice}）。")
        return []

    target = workspace / _safe_name(request.get("filename"), "朗读.mp3", ".mp3")
    fmt = target.suffix.lstrip(".").lower() or "mp3"
    if fmt not in {"mp3", "wav", "flac", "opus", "pcm"}:
        fmt = "mp3"
        target = target.with_suffix(".mp3")

    cmd = [
        mmx, "speech", "synthesize",
        "--text", text,
        "--voice", voice,
        "--format", fmt,
        "--out", str(target),
        "--quiet", "--non-interactive",
    ]
    if abs(speed - 1.0) > 0.01:
        cmd += ["--speed", f"{speed:g}"]
    if emotion:
        cmd += ["--emotion", emotion]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PATH": f"{Path(mmx).parent}:{os.environ.get('PATH', '')}"},
        )
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=settings.mmx_timeout_s)
    except asyncio.TimeoutError:
        notices.append(f"⚠️ 语音生成超时（超过 {settings.mmx_timeout_s:.0f} 秒）。")
        return []
    except Exception:  # noqa: BLE001
        logger.warning("mmx 语音合成失败", exc_info=True)
        notices.append("⚠️ 语音生成失败：无法启动 mmx。")
        return []
    if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        detail = (err or b"").decode(errors="replace").strip().splitlines()
        notices.append(f"⚠️ 语音生成失败：{detail[-1] if detail else 'mmx 未返回音频'}。")
        return []
    return [target]


async def _generate_image(
    *, workspace: Path, settings: Settings, request: dict, notices: list[str]
) -> list[Path]:
    prompt = str(request.get("prompt") or "").strip()
    if not prompt:
        notices.append("⚠️ 图片生成失败：`.image_request.json` 里缺少 prompt。")
        return []
    prompt = prompt[:_IMAGE_MAX_PROMPT]
    try:
        count = int(request.get("n") or 1)
    except (TypeError, ValueError):
        count = 1
    count = min(max(count, 1), _IMAGE_MAX_COUNT)
    aspect = str(request.get("aspect_ratio") or "").strip()
    base_name = _safe_name(request.get("filename"), "生成图.jpg", ".jpg")
    stem = Path(base_name).stem

    mmx = settings.mmx_bin
    if not mmx or not Path(mmx).exists():
        notices.append("⚠️ 图片生成失败：服务器上找不到 mmx CLI。")
        return []

    outputs: list[Path] = []
    for index in range(count):
        name = f"{stem}.jpg" if count == 1 else f"{stem}_{index + 1}.jpg"
        target = workspace / name
        cmd = [
            mmx, "image", "generate",
            "--prompt", prompt,
            "--out", str(target),
            "--quiet", "--non-interactive", "--output", "json",
        ]
        if aspect:
            cmd += ["--aspect-ratio", aspect]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PATH": f"{Path(mmx).parent}:{os.environ.get('PATH', '')}"},
            )
            _out, err = await asyncio.wait_for(
                proc.communicate(), timeout=settings.mmx_timeout_s
            )
        except asyncio.TimeoutError:
            notices.append(f"⚠️ 图片生成超时（超过 {settings.mmx_timeout_s:.0f} 秒）。")
            break
        except Exception:  # noqa: BLE001
            logger.warning("mmx 调用失败", exc_info=True)
            notices.append("⚠️ 图片生成失败：无法启动 mmx。")
            break
        if proc.returncode != 0 or not target.is_file():
            detail = (err or b"").decode(errors="replace").strip().splitlines()
            notices.append(f"⚠️ 图片生成失败：{detail[-1] if detail else 'mmx 未返回文件'}。")
            break
        # mmx 实际输出的是 JPEG，哪怕文件名写成 .png —— 按真实格式纠正扩展名
        real_suffix = _sniff_suffix(target)
        if real_suffix and real_suffix != target.suffix.lower():
            corrected = target.with_suffix(real_suffix)
            target.replace(corrected)
            target = corrected
        outputs.append(target)
    return outputs


async def process_requests(
    workspace: Path,
    settings: Settings,
    *,
    material_files: dict[str, str] | None = None,
    notify=None,  # noqa: ANN001 - async callable(str) -> None
) -> GenerationResult:
    """执行工作区里的生成请求；返回产物文件与需要告知用户的说明。"""
    result = GenerationResult()
    for filename in (TTS_REQUEST_FILE, IMAGE_REQUEST_FILE, VIDEO_REQUEST_FILE):
        request_path = workspace / filename
        if not request_path.is_file():
            continue
        try:
            raw = json.loads(request_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("请求必须是 JSON 对象")
        except Exception as exc:  # noqa: BLE001
            result.notices.append(f"⚠️ 生成请求 `{filename}` 解析失败：{exc}")
            request_path.unlink(missing_ok=True)
            continue
        try:
            if filename == TTS_REQUEST_FILE:
                produced = await _synthesize_speech(
                    workspace=workspace, settings=settings, request=raw, notices=result.notices
                )
            elif filename == IMAGE_REQUEST_FILE:
                produced = await _generate_image(
                    workspace=workspace, settings=settings, request=raw, notices=result.notices
                )
            else:
                if notify:
                    await notify("正在提交视频生成任务…")
                raw["prompt"] = str(raw.get("prompt") or "")[:_VIDEO_MAX_PROMPT]
                produced = await video_gen.generate_video(
                    workspace=workspace,
                    settings=settings,
                    request=raw,
                    source_files=material_files,
                    notify=notify,
                    notices=result.notices,
                )
            result.files.extend(produced)
        finally:
            # 无论成功失败都删掉请求文件，避免下一轮被重复执行
            request_path.unlink(missing_ok=True)
    return result
