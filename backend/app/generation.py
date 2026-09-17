"""模型发起的"生成任务"：语音合成（IndexTTS）与图片生成（mmx CLI）。

为什么用"请求文件"而不是让模型直接调服务：
codex 的 shell 跑在沙箱里、**没有网络**（连 127.0.0.1 的服务也连不上），所以模型无法自己调用
本机 TTS 或 mmx CLI。约定：模型在会话工作区根目录写一个请求 JSON，后端在这一轮结束时执行、
把产物写回工作区，随后产出物链路自动把它送到聊天窗口和详情页。

请求文件（都是隐藏文件，不会被登记成产出物）：
- `.tts_request.json`   {"text": "...", "filename": "朗读.wav", "voice": "default",
                        "pace": "narration|reading", "speed": 0.9, "pauses": {"sentence": 0.75}}
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
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import Settings
from . import video_gen

logger = logging.getLogger(__name__)

TTS_REQUEST_FILE = ".tts_request.json"
IMAGE_REQUEST_FILE = ".image_request.json"
VIDEO_REQUEST_FILE = ".video_request.json"
RENDER_REQUEST_FILE = ".render_request.json"

_TTS_MAX_CHARS = 2000
_IMAGE_MAX_PROMPT = 800
_IMAGE_MAX_COUNT = 4
_VIDEO_MAX_PROMPT = 1500
# mmx 音色库里不少名字带空格/括号（如 "Chinese (Mandarin)_Wise_Women"），
# 这里只做"不像命令行参数"的白名单校验——参数是 exec 直传的，不经 shell。
_VOICE_ID_RE = re.compile(r"^(?!-)[A-Za-z0-9 _().-]{2,60}$")
_SAFE_NAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")

# ---- 旁白的语速与留白 -------------------------------------------------------
# 教学旁白"听着赶"是两个原因叠加：
#   1) 本机 TTS 服务（IndexTTS）**忽略 speed 参数**，传 0.9 也按原速合成；
#   2) 整段文本一次性合成，句子之间没有停顿。
# 所以语速由后端做时间拉伸（rubberband/atempo，保音高），停顿由"分句合成 + 插静音"实现。
_TTS_PACE_VERSION = "pace-v1"  # 进缓存 key：以后改节奏算法时旧缓存自动失效
_DEFAULT_SAMPLE_RATE = 24000  # IndexTTS 输出 24k 单声道；探测失败时按它兜底

# 两档默认节奏（speed < 1 = 更慢）。讲解类默认慢一档、句间留白更长。
PACE_PRESETS: dict[str, dict[str, float]] = {
    "reading": {"speed": 1.0, "sentence": 0.45, "paragraph": 1.0, "tail": 0.0},
    "narration": {"speed": 0.85, "sentence": 0.8, "paragraph": 1.4, "tail": 0.5},
}
# 旁白里可以显式标停顿：[[pause:1.2]] / [[停顿1.2]] / [[pause]]（默认 1 秒）
_PAUSE_MARKER_RE = re.compile(
    r"\[\[\s*(?:pause|停顿)\s*:?\s*([0-9]*\.?[0-9]+)?\s*\]\]", re.IGNORECASE
)
# 断句用：句末标点（含收尾引号/括号），逗号保持句内不断开
_SENTENCE_SPLIT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]*[”’\"）)]*")
# 掐掉每句首尾自带静音（倒放再掐头 = 掐尾），只留 50ms 余韵，避免削掉尾音
_TRIM_SILENCE_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05:detection=peak,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05:detection=peak,"
    "areverse"
)


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


def _pace_settings(request: dict) -> dict[str, float]:
    """合并"节奏档位 + 显式覆盖"，得到 {speed, sentence, paragraph, tail}。

    - `pace`: "narration"（讲解，默认慢一档）/ "reading"（朗读，原速）
    - `speed`: 显式语速，覆盖档位（0.5~2.0，<1 更慢）
    - `pauses`: {"sentence": 0.75, "paragraph": 1.4, "tail": 0.5} 显式覆盖留白
    """
    preset = str(request.get("pace") or "").strip().lower()
    pace = dict(PACE_PRESETS.get(preset, PACE_PRESETS["reading"]))
    if request.get("speed") is not None:
        try:
            pace["speed"] = float(request["speed"])
        except (TypeError, ValueError):
            pass
    pace["speed"] = min(max(float(pace["speed"]), 0.5), 2.0)
    pauses = request.get("pauses")
    if isinstance(pauses, dict):
        for key in ("sentence", "paragraph", "tail"):
            if pauses.get(key) is not None:
                try:
                    pace[key] = max(0.0, min(float(pauses[key]), 10.0))
                except (TypeError, ValueError):
                    pass
    return pace


def split_narration(text: str, pace: dict[str, float]) -> list[tuple[str, float]]:
    """把旁白切成 [(句子, 这句之后留白多少秒)]——只决定停顿，不改一个字。

    规则：句末标点断句（逗号不断），空行算段末（留白更长），
    `[[pause:1.2]]` 可以手工指定某句后面停多久（`[[pause]]` = 1 秒）。
    """
    sentence_gap = float(pace.get("sentence", 0.0))
    paragraph_gap = float(pace.get("paragraph", sentence_gap))
    tail_gap = float(pace.get("tail", 0.0))

    def cut(body: str) -> list[str]:
        found = [s.strip() for s in _SENTENCE_SPLIT_RE.findall(body) if s.strip()]
        return found or ([body.strip()] if body.strip() else [])

    chunks: list[list] = []
    paragraphs = [p for p in re.split(r"\n\s*\n+", text) if p.strip()]
    for p_index, paragraph in enumerate(paragraphs):
        pieces = _PAUSE_MARKER_RE.split(paragraph)
        # split 结果：[正文, 标记里的秒数或 None, 正文, ...]
        for index in range(0, len(pieces), 2):
            marker_gap: float | None = None
            if index + 1 < len(pieces):
                raw = pieces[index + 1]
                marker_gap = float(raw) if raw else 1.0
            sentences = cut(pieces[index])
            if not sentences and marker_gap is not None and chunks:
                # 标记写在句子前面（"[[pause:1.5]]下一句…"）：算作它前面那句之后的停顿
                chunks[-1][1] = max(chunks[-1][1], marker_gap)
                continue
            for s_index, sentence in enumerate(sentences):
                last = s_index == len(sentences) - 1
                gap = marker_gap if (last and marker_gap is not None) else sentence_gap
                chunks.append([sentence, gap])
        if p_index < len(paragraphs) - 1 and chunks:
            chunks[-1][1] = max(chunks[-1][1], paragraph_gap)
    if chunks:
        chunks[-1][1] = tail_gap  # 结尾留一口气，别切在最后一个字上
    return [(str(body), max(0.0, float(gap))) for body, gap in chunks]


async def _fetch_speech_audio(
    *, settings: Settings, text: str, voice: str, cache_dir: Path, notices: list[str]
) -> bytes | None:
    """向本机 TTS 服务要一段音频，带磁盘缓存（分句合成因此很省时间）。"""
    key = hashlib.sha256(f"{_TTS_PACE_VERSION}|{text}|{voice}".encode()).hexdigest()
    cached = cache_dir / f"{key}.wav"
    if cached.is_file():
        return cached.read_bytes()
    try:
        async with httpx.AsyncClient(timeout=settings.tts_generation_timeout_s) as http:
            resp = await http.post(
                f"{settings.tts_base_url}/api/preview",
                json={"text": text, "tts_engine": "index_tts", "voice": voice},
            )
        if resp.status_code != 200:
            notices.append(f"⚠️ 语音生成失败：TTS 服务返回 {resp.status_code}。")
            return None
        data = resp.json()
        if not data.get("success") or not data.get("audio_base64"):
            notices.append(f"⚠️ 语音生成失败：{data.get('error') or '服务未返回音频'}。")
            return None
        audio = base64.b64decode(data["audio_base64"])
        cached.write_bytes(audio)
        return audio
    except httpx.HTTPError as exc:
        logger.warning("TTS 服务不可用: %s", exc)
        notices.append("⚠️ 语音生成失败：本机语音合成服务不可用。")
        return None
    except Exception:  # noqa: BLE001
        logger.warning("语音生成异常", exc_info=True)
        notices.append("⚠️ 语音生成失败：内部错误。")
        return None


async def _run_ffmpeg(ffmpeg: str, args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode, (out or b"").decode(errors="replace")


async def _probe_audio_format(path: Path, settings: Settings) -> tuple[int, int]:
    """返回 (采样率, 声道数)；探测不出来就给 (24000, 1)。"""
    ffprobe = settings.ffmpeg_bin.replace("ffmpeg", "ffprobe")
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels",
            "-of", "default=nw=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        info = dict(line.split("=", 1) for line in out.decode().splitlines() if "=" in line)
        return int(info.get("sample_rate", _DEFAULT_SAMPLE_RATE)), int(info.get("channels", 1))
    except Exception:  # noqa: BLE001 - 探测失败就用默认值
        return _DEFAULT_SAMPLE_RATE, 1


async def _assemble_narration(
    *,
    settings: Settings,
    pieces: list[bytes],
    gaps: list[float],
    speed: float,
    target: Path,
    work_dir: Path,
) -> bool:
    """分句音频 → 插静音 → 保音高变速 → 写成一条 wav。失败返回 False。"""
    if not pieces:
        return False
    ffmpeg = settings.ffmpeg_bin

    raw_first = work_dir / "piece_000.raw"
    raw_first.write_bytes(pieces[0])
    rate, channels = await _probe_audio_format(raw_first, settings)
    layout = "mono" if channels <= 1 else "stereo"

    ordered: list[Path] = []
    for index, data in enumerate(pieces):
        raw = work_dir / f"piece_{index:03d}.raw"
        raw.write_bytes(data)
        norm = work_dir / f"piece_{index:03d}.wav"
        # 先掐掉每句自带的首尾静音：TTS 每条音频尾巴常带 0.3~0.6 秒空白，
        # 不掐掉就会"设计放 0.75 秒、实际静了 1.5 秒"，节奏全靠运气。
        code, log = await _run_ffmpeg(ffmpeg, [
            "-y", "-v", "error", "-i", str(raw),
            "-filter:a", _TRIM_SILENCE_FILTER,
            "-ar", str(rate), "-ac", str(channels), "-c:a", "pcm_s16le", str(norm),
        ])
        if code != 0 or not norm.is_file():
            logger.warning("旁白分句转码失败: %s", log[-300:])
            return False
        ordered.append(norm)
        raw.unlink(missing_ok=True)
        gap = float(gaps[index]) if index < len(gaps) else 0.0
        if gap >= 0.02:
            silence = work_dir / f"silence_{index:03d}.wav"
            code, log = await _run_ffmpeg(ffmpeg, [
                "-y", "-v", "error", "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl={layout}",
                "-t", f"{gap:.3f}", "-ar", str(rate), "-ac", str(channels),
                "-c:a", "pcm_s16le", str(silence),
            ])
            if code != 0 or not silence.is_file():
                logger.warning("静音生成失败: %s", log[-300:])
                return False
            ordered.append(silence)

    list_file = work_dir / "concat.txt"
    list_file.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in ordered), encoding="utf-8"
    )
    concat_input = ["-f", "concat", "-safe", "0", "-i", str(list_file)]
    # 输出编码按目标扩展名：wav 留 PCM（配音可直接复用），mp3 转回 MP3
    if target.suffix.lower() == ".mp3":
        out_args = ["-ar", str(rate), "-ac", str(channels), "-c:a", "libmp3lame",
                    "-b:a", "160k", str(target)]
    else:
        out_args = ["-ar", str(rate), "-ac", str(channels), "-c:a", "pcm_s16le", str(target)]
    if abs(speed - 1.0) > 0.01:
        # rubberband 保音高更自然；没有它时退回 atempo（0.5~2.0 单级够用）
        produced = False
        for tempo in (f"rubberband=tempo={speed:g}", f"atempo={speed:g}"):
            code, log = await _run_ffmpeg(
                ffmpeg, ["-y", "-v", "error", *concat_input, "-filter:a", tempo, *out_args]
            )
            if code == 0 and target.is_file() and target.stat().st_size > 0:
                produced = True
                break
            logger.warning("旁白变速失败(%s): %s", tempo, log[-300:])
        if not produced:
            return False
    else:
        code, log = await _run_ffmpeg(ffmpeg, ["-y", "-v", "error", *concat_input, *out_args])
        if code != 0 or not target.is_file() or target.stat().st_size == 0:
            logger.warning("旁白拼接失败: %s", log[-300:])
            return False
    # 响度统一：换音色/换引擎时音量不飘（失败不影响成片，只是音量不统一）
    await _loudness_normalize(
        target, settings=settings, work_dir=work_dir, rate=rate, channels=channels
    )
    return True


_LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


async def _loudness_normalize(
    path: Path, *, settings: Settings, work_dir: Path, rate: int, channels: int
) -> bool:
    """把整段旁白响度归一到目标 LUFS。

    为什么要它：不同音色/引擎的原始音量差很多（实测同一句话「温暖闺蜜」比其它
    音色低约 9dB），不统一就会出现"换个音色，声音忽然变小"。
    用两遍法（先量后套用 measured_*），避免单遍 loudnorm 的动态增益瑕疵。
    """
    target_lufs = settings.narration_loudness_lufs
    if target_lufs >= -1 or target_lufs <= -60:  # 0 或异常值 = 关闭
        return False
    ffmpeg = settings.ffmpeg_bin
    base_filter = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
    code, log = await _run_ffmpeg(ffmpeg, [
        "-v", "info", "-i", str(path),
        "-af", f"{base_filter}:print_format=json",
        "-f", "null", "-",
    ])
    match = _LOUDNORM_JSON_RE.search(log) if code == 0 else None
    if not match:
        logger.warning("响度测量失败，跳过归一化")
        return False
    try:
        data = json.loads(match.group(0))
        measured = "".join(
            f":measured_{key}={data[field]}"
            for key, field in (
                ("I", "input_i"), ("TP", "input_tp"),
                ("LRA", "input_lra"), ("thresh", "input_thresh"),
            )
        )
    except (ValueError, KeyError):
        logger.warning("响度测量结果解析失败，跳过归一化")
        return False
    temp = work_dir / f"loudness{path.suffix}"
    if path.suffix.lower() == ".mp3":
        out_args = ["-c:a", "libmp3lame", "-b:a", "160k"]
    else:
        out_args = ["-c:a", "pcm_s16le"]
    code, log = await _run_ffmpeg(ffmpeg, [
        "-y", "-v", "error", "-i", str(path),
        "-af", f"{base_filter}{measured}:linear=true",
        "-ar", str(rate), "-ac", str(channels), *out_args, str(temp),
    ])
    if code != 0 or not temp.is_file() or temp.stat().st_size == 0:
        logger.warning("响度归一化失败: %s", log[-300:])
        return False
    shutil.move(str(temp), str(path))
    return True


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
    pace = _pace_settings(request)
    speed = pace["speed"]
    emotion = str(request.get("emotion") or "").strip()

    cache_dir = settings.data_dir / "tts_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 引擎二选一（两者都走同一条"分句 + 留白 + 保音高变速"的节奏流水线）：
    #   index_tts（默认）= 本机 IndexTTS-2.5，音色固定、中文自然
    #   mmx              = MiniMax 云端音色库，可选音色/情感（几十个中文音色）
    if engine in ("mmx", "minimax"):
        target = workspace / _safe_name(request.get("filename"), "朗读.mp3", ".mp3")
        fmt = target.suffix.lstrip(".").lower() or "mp3"
        if fmt not in {"mp3", "wav", "flac", "opus", "pcm"}:
            fmt = "mp3"
            target = target.with_suffix(".mp3")

        async def fetch(chunk: str) -> bytes | None:
            return await _fetch_mmx_audio(
                settings=settings, text=chunk, voice=voice, emotion=emotion,
                fmt=fmt, cache_dir=cache_dir, notices=notices,
            )

        async def local_fallback(chunk: str) -> bytes | None:
            """云端音色不可用（断网/额度）时退回本机音色，别让讲解视频没声音。"""
            return await _fetch_speech_audio(
                settings=settings, text=chunk, voice="default", cache_dir=cache_dir, notices=notices
            )

        fallbacks = [local_fallback]
    else:
        target = workspace / _safe_name(request.get("filename"), "朗读.wav", ".wav")

        async def fetch(chunk: str) -> bytes | None:
            return await _fetch_speech_audio(
                settings=settings, text=chunk, voice=voice, cache_dir=cache_dir, notices=notices
            )

        fallbacks = []

    used_fallback = {"hit": False}

    async def fetch_any_voice(chunk: str) -> bytes | None:
        """整段级别的取音：先用指定音色，云端不可用时退回本机音色（不逐句混音色）。"""
        audio = await fetch(chunk)
        if audio is not None or not fallbacks:
            return audio
        audio = await fallbacks[0](chunk)
        if audio is not None:
            used_fallback["hit"] = True
        return audio

    def announce_fallback() -> None:
        if used_fallback["hit"]:
            notices.append("⚠️ 云端音色不可用，这段旁白用了本机音色。")

    units = split_narration(text, pace)
    # 从这里开始的失败提示，只要最终出了音频就撤掉（用户不必知道中间试过什么）
    mark = len(notices)
    # 单句 + 原速 + 无留白：直接落原始音频（不必过 ffmpeg）
    if len(units) == 1 and abs(speed - 1.0) <= 0.01 and units[0][1] <= 0.01:
        audio = await fetch_any_voice(text)
        if audio is None:
            return []
        del notices[mark:]
        announce_fallback()
        target.write_bytes(audio)
        return [target]

    # 分句合成 → 句间插静音 → 按 speed 保音高变速
    pieces: list[bytes] = []
    for sentence, _gap in units:
        audio = await fetch(sentence)
        if audio is None:
            pieces = []
            break
        pieces.append(audio)

    if pieces:
        work_dir = cache_dir / f"work-{uuid.uuid4().hex}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            done = await _assemble_narration(
                settings=settings,
                pieces=pieces,
                gaps=[gap for _text, gap in units],
                speed=speed,
                target=target,
                work_dir=work_dir,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        if done:
            del notices[mark:]
            return [target]
        notices.append("⚠️ 旁白节奏处理失败，已退回整段合成（停顿会少一些）。")

    # 兜底：整段一次合成（至少保住语速）
    audio = await fetch_any_voice(text)
    if audio is None:
        return []
    if abs(speed - 1.0) <= 0.01:
        del notices[mark:]
        announce_fallback()
        target.write_bytes(audio)
        return [target]
    work_dir = cache_dir / f"work-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        done = await _assemble_narration(
            settings=settings,
            pieces=[audio],
            gaps=[0.0],
            speed=speed,
            target=target,
            work_dir=work_dir,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    if not done:
        target.write_bytes(audio)
    del notices[mark:]
    announce_fallback()
    return [target]


async def _fetch_mmx_audio(
    *,
    settings: Settings,
    text: str,
    voice: str,
    emotion: str,
    fmt: str,
    cache_dir: Path,
    notices: list[str],
) -> bytes | None:
    """用 mmx CLI 合成一小段语音并返回字节（带缓存）。语速由外层统一处理。"""
    mmx = settings.mmx_bin
    if not mmx or not Path(mmx).exists():
        notices.append("⚠️ 语音生成失败：服务器上找不到 mmx CLI。")
        return None
    if not _VOICE_ID_RE.fullmatch(voice):
        notices.append(f"⚠️ 语音生成失败：音色名不合法（{voice}）。")
        return None

    key = hashlib.sha256(
        f"{_TTS_PACE_VERSION}|mmx|{text}|{voice}|{emotion}|{fmt}".encode()
    ).hexdigest()
    cached = cache_dir / f"{key}.{fmt}"
    if cached.is_file() and cached.stat().st_size:
        return cached.read_bytes()

    out_path = cache_dir / f"mmx-{uuid.uuid4().hex}.{fmt}"
    cmd = [
        mmx, "speech", "synthesize",
        "--text", text,
        "--voice", voice,
        "--format", fmt,
        "--out", str(out_path),
        "--quiet", "--non-interactive",
    ]
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
        return None
    except Exception:  # noqa: BLE001
        logger.warning("mmx 语音合成失败", exc_info=True)
        notices.append("⚠️ 语音生成失败：无法启动 mmx。")
        return None
    if proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        detail = (err or b"").decode(errors="replace").strip().splitlines()
        notices.append(f"⚠️ 语音生成失败：{detail[-1] if detail else 'mmx 未返回音频'}。")
        return None
    audio = out_path.read_bytes()
    out_path.unlink(missing_ok=True)
    cached.write_bytes(audio)
    return audio


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
    for filename in (
        TTS_REQUEST_FILE,
        IMAGE_REQUEST_FILE,
        VIDEO_REQUEST_FILE,
        RENDER_REQUEST_FILE,
    ):
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
            elif filename == VIDEO_REQUEST_FILE:
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
            else:
                from . import render  # 局部导入避免循环依赖

                produced = await render.render_composition(
                    workspace=workspace,
                    settings=settings,
                    request=raw,
                    notify=notify,
                    notices=result.notices,
                )
            result.files.extend(produced)
        finally:
            # 无论成功失败都删掉请求文件，避免下一轮被重复执行
            request_path.unlink(missing_ok=True)
    return result
