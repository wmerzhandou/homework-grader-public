"""MiniMax H3 视频生成（走 ComfyUI HTTP API）。

模板来自 /opt/openmontage/assets/workflows/h3_t2v_api.json（文生视频）与
h3_r2v_api.json（参考图生视频），输出节点是 SaveVideo(92)，
history 里形如 {"images": [{"filename": "xxx.mp4", "subfolder": "video", "type": "output"}]}，
再用 /view 把文件取回来。

时长（秒）由模板里的 ComfyMathExpression 按 24fps 换算，我们只要改喂进去的那个 PrimitiveFloat。
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import uuid
from pathlib import Path

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

# 分辨率档位（宽高必须是 32 的倍数，跟模板里的 864x480 对齐）
_RESOLUTIONS: dict[str, dict[str, tuple[int, int]]] = {
    "480p": {"16:9": (864, 480), "9:16": (480, 864), "1:1": (640, 640)},
    "768p": {"16:9": (1344, 768), "9:16": (768, 1344), "1:1": (1024, 1024)},
}
_POLL_INTERVAL_S = 5.0
_SAFE_NAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


def _safe_video_name(raw: str | None) -> str:
    name = (raw or "").strip() or "生成视频.mp4"
    name = _SAFE_NAME_RE.sub("_", name).lstrip(".") or "生成视频"
    return name if name.lower().endswith(".mp4") else f"{Path(name).stem}.mp4"


def _clamp_seconds(value: object, default: float = 5.0) -> float:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        seconds = default
    return min(max(seconds, 2.0), 15.0)


def _pick_resolution(resolution: str | None, aspect: str | None) -> tuple[int, int, str]:
    tier = (resolution or "480p").strip().lower()
    if tier not in _RESOLUTIONS:
        tier = "480p"
    aspect_key = (aspect or "16:9").strip()
    if aspect_key not in _RESOLUTIONS[tier]:
        aspect_key = "16:9"
    width, height = _RESOLUTIONS[tier][aspect_key]
    return width, height, aspect_key


def _load_workflow(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"工作流不是 JSON 对象：{path}")
    return data


async def _upload_reference(
    http: httpx.AsyncClient, image_path: Path
) -> str:
    """把参考图上传到 ComfyUI 的 input 目录，返回它认的文件名。"""
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
    files = {"image": (image_path.name, image_path.read_bytes(), mime)}
    resp = await http.post("/upload/image", files=files, data={"overwrite": "true"})
    resp.raise_for_status()
    payload = resp.json()
    name = payload.get("name") or image_path.name
    subfolder = payload.get("subfolder") or ""
    return f"{subfolder}/{name}" if subfolder else name


async def generate_video(
    *,
    workspace: Path,
    settings: Settings,
    request: dict,
    source_files: dict[str, str] | None = None,
    notify=None,  # noqa: ANN001 - async callable(str) -> None
    notices: list[str],
) -> list[Path]:
    prompt = str(request.get("prompt") or "").strip()
    if not prompt:
        notices.append("⚠️ 视频生成失败：`.video_request.json` 里缺少 prompt。")
        return []

    seconds = _clamp_seconds(request.get("seconds"))
    width, height, aspect_key = _pick_resolution(request.get("resolution"), request.get("aspect_ratio"))
    reference = str(request.get("reference") or "").strip()
    target = workspace / _safe_video_name(request.get("filename"))

    # 参考图：支持"上传的材料文件名"或工作区里的图片
    reference_path: Path | None = None
    if reference:
        candidates = [workspace / reference]
        if source_files and reference in source_files:
            candidates.insert(0, Path(source_files[reference]))
        for candidate in candidates:
            if candidate.is_file():
                reference_path = candidate
                break
        if reference_path is None:
            notices.append(f"⚠️ 视频生成失败：找不到参考图「{reference}」。")
            return []

    workflow_path = settings.h3_r2v_workflow if reference_path else settings.h3_t2v_workflow
    try:
        workflow = _load_workflow(workflow_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("加载 H3 工作流失败: %s", exc)
        notices.append("⚠️ 视频生成失败：服务器上找不到 H3 工作流模板。")
        return []

    seed = request.get("seed")
    try:
        seed_value = int(seed) if seed is not None else None
    except (TypeError, ValueError):
        seed_value = None

    if reference_path is None:
        # T2V：节点 131 是 MiniMaxH3ImageToVideo，133 是时长（秒），129 是种子
        node = workflow.get("131")
        if not node:
            notices.append("⚠️ 视频生成失败：工作流模板结构不符合预期。")
            return []
        node.setdefault("inputs", {}).update({"prompt": prompt, "width": width, "height": height})
        if "133" in workflow:
            workflow["133"].setdefault("inputs", {})["value"] = seconds
        if seed_value is not None and "129" in workflow:
            workflow["129"].setdefault("inputs", {})["noise_seed"] = seed_value
    else:
        # R2V：138 是提示词，137 是参考图，115 选比例，132 是时长
        if "138" not in workflow or "137" not in workflow:
            notices.append("⚠️ 视频生成失败：R2V 工作流模板结构不符合预期。")
            return []
        workflow["138"].setdefault("inputs", {})["value"] = prompt
        if "115" in workflow:
            aspect_label = {"16:9": "16:9 (Widescreen)", "9:16": "9:16 (Portrait)", "1:1": "1:1 (Square)"}
            workflow["115"].setdefault("inputs", {})["aspect_ratio"] = aspect_label.get(
                aspect_key, "16:9 (Widescreen)"
            )
        if "132" in workflow:
            workflow["132"].setdefault("inputs", {})["value"] = seconds
        if seed_value is not None and "129" in workflow:
            workflow["129"].setdefault("inputs", {})["noise_seed"] = seed_value

    base = settings.comfyui_base_url
    timeout = httpx.Timeout(settings.video_generation_timeout_s, connect=15.0)
    try:
        async with httpx.AsyncClient(base_url=base, timeout=timeout) as http:
            if reference_path is not None:
                uploaded = await _upload_reference(http, reference_path)
                workflow["137"].setdefault("inputs", {})["image"] = uploaded

            if notify:
                await notify(f"视频生成已提交（{seconds:.0f} 秒 {width}x{height}），预计 1-5 分钟…")
            resp = await http.post(
                "/prompt", json={"prompt": workflow, "client_id": uuid.uuid4().hex}
            )
            if resp.status_code != 200:
                detail = resp.text[:300]
                notices.append(f"⚠️ 视频生成失败：农场返回 {resp.status_code} {detail}")
                return []
            payload = resp.json()
            if payload.get("node_errors"):
                notices.append(f"⚠️ 视频生成失败：工作流校验不通过（{list(payload['node_errors'])[:3]}）。")
                return []
            prompt_id = payload.get("prompt_id")
            if not prompt_id:
                notices.append("⚠️ 视频生成失败：农场未返回任务 ID。")
                return []

            # 轮询直到完成
            elapsed = 0.0
            entry: dict | None = None
            while elapsed < settings.video_generation_timeout_s:
                await asyncio.sleep(_POLL_INTERVAL_S)
                elapsed += _POLL_INTERVAL_S
                history = (await http.get(f"/history/{prompt_id}")).json()
                entry = history.get(prompt_id)
                if entry:
                    status = ((entry.get("status") or {}).get("status_str") or "").lower()
                    if status in ("error", "failed"):
                        notices.append("⚠️ 视频生成失败：农场侧执行出错。")
                        return []
                    break
                if notify and int(elapsed) % 30 == 0:
                    await notify(f"视频生成中…已等待 {int(elapsed)} 秒")

            if entry is None:
                notices.append(f"⚠️ 视频生成超时（超过 {settings.video_generation_timeout_s:.0f} 秒）。")
                return []

            outputs = entry.get("outputs") or {}
            file_info = None
            for node_output in outputs.values():
                items = node_output.get("images") or []
                for item in items:
                    if str(item.get("filename", "")).lower().endswith((".mp4", ".webm", ".mov")):
                        file_info = item
                        break
                if file_info:
                    break
            if file_info is None:
                notices.append("⚠️ 视频生成完成，但没找到输出文件。")
                return []

            if notify:
                await notify("视频生成完成，正在取回文件…")
            view = await http.get(
                "/view",
                params={
                    "filename": file_info["filename"],
                    "subfolder": file_info.get("subfolder", ""),
                    "type": file_info.get("type", "output"),
                },
            )
            if view.status_code != 200 or not view.content:
                notices.append("⚠️ 视频生成完成，但下载失败。")
                return []
            target.write_bytes(view.content)
    except httpx.HTTPError as exc:
        logger.warning("H3 农场调用失败: %s", exc)
        notices.append("⚠️ 视频生成失败：连不上 H3 农场。")
        return []
    except Exception:  # noqa: BLE001
        logger.warning("视频生成异常", exc_info=True)
        notices.append("⚠️ 视频生成失败：内部错误。")
        return []

    return [target]
