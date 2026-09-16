"""Per-user codex app-server process manager with lazy spawn and idle reaping.

Each user gets a dedicated `codex app-server` child process with its own
CODEX_HOME (process-level isolation; app-server has no multi-tenancy).
Processes idle for longer than `codex_idle_timeout_s` are terminated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import CodexConfig
from openai_codex.errors import InvalidRequestError
from openai_codex.generated.v2_all import AgentMessageDeltaNotification, TurnCompletedNotification
from openai_codex.generated.v2_all import ItemCompletedNotification, ItemStartedNotification
from sqlmodel import Session, select

from .. import artifacts as artifacts_service
from .. import events
from .. import generation
from .. import media_prep
from ..config import Settings, get_settings
from ..db import get_engine
from ..mistakes import record_mistakes
from ..models import (
    RESULT_TYPE_ENGLISH_PASSAGE,
    RESULT_TYPE_GRADING,
    Material,
    MaterialKind,
    MaterialStatus,
    Message,
    Thread,
)
from ..models import Artifact
from ..pipeline.images import detect_kind
from . import context, workspace
from .grading import load_english_result, load_grading_result

logger = logging.getLogger(__name__)

_REAPER_INTERVAL_S = 60.0
# 等待材料预处理完成的最长时间（秒）
_MATERIAL_WAIT_S = 90.0


def _item_fields(item: object) -> dict:
    """把通知里的 item 统一成 dict 读取。

    协议里 item 是联合类型，属性访问在部分路径上拿不到值（type/phase 为 None），
    model_dump() 更可靠，所以统一走它，取不到再退回 __dict__。
    """
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001 - 兜底，不影响主流程
            pass
    raw = getattr(item, "__dict__", None)
    return raw if isinstance(raw, dict) else {}


def _phase_value(phase: object) -> str | None:
    if phase is None:
        return None
    return getattr(phase, "value", None) or (phase if isinstance(phase, str) else None)


@dataclass
class _UserRuntime:
    client: AsyncCodexClient
    last_used: float = field(default_factory=time.monotonic)
    # codex thread ids known to be live in THIS app-server process.
    # After a reap+respawn the set is empty, so stored threads get resumed.
    live_threads: set[str] = field(default_factory=set)


class CodexManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._runtimes: dict[str, _UserRuntime] = {}
        self._spawn_guard = asyncio.Lock()
        self._thread_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._reaper_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # process lifecycle
    # ------------------------------------------------------------------
    async def _spawn(self, user_id: str) -> _UserRuntime:
        codex_home = workspace.ensure_user_codex_home(self.settings, user_id)
        client = AsyncCodexClient(
            config=CodexConfig(
                codex_bin=self.settings.codex_bin,
                env={"CODEX_HOME": str(codex_home)},
                client_name="homework_grader",
                client_title="Homework Grader Backend",
            )
        )
        await client.start()
        await client.initialize()
        logger.info("spawned codex app-server for user %s", user_id)
        return _UserRuntime(client=client)

    async def get_runtime(self, user_id: str) -> _UserRuntime:
        async with self._spawn_guard:
            runtime = self._runtimes.get(user_id)
            if runtime is None:
                runtime = await self._spawn(user_id)
                self._runtimes[user_id] = runtime
            runtime.last_used = time.monotonic()
            return runtime

    async def get_client(self, user_id: str) -> AsyncCodexClient:
        return (await self.get_runtime(user_id)).client

    async def _drop_runtime(self, user_id: str) -> None:
        runtime = self._runtimes.pop(user_id, None)
        if runtime is None:
            return
        try:
            await runtime.client.close()
        except Exception:
            logger.exception("error closing codex app-server for user %s", user_id)

    async def reap_idle(self) -> None:
        now = time.monotonic()
        idle_users = [
            uid
            for uid, rt in self._runtimes.items()
            if now - rt.last_used > self.settings.codex_idle_timeout_s
        ]
        for user_id in idle_users:
            logger.info("reaping idle codex app-server for user %s", user_id)
            await self._drop_runtime(user_id)

    def start_reaper(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(_REAPER_INTERVAL_S)
            try:
                await self.reap_idle()
            except Exception:
                logger.exception("codex reaper iteration failed")

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
        for user_id in list(self._runtimes):
            await self._drop_runtime(user_id)

    # ------------------------------------------------------------------
    # turn execution
    # ------------------------------------------------------------------
    async def run_turn(
        self,
        user_id: str,
        thread_id: str,
        turn_id: str,
        text: str,
        material_ids: list[str] | None,
        reference_ids: list[str] | None = None,
        unresolved_codes: list[str] | None = None,
    ) -> None:
        """Execute one codex turn in the background, streaming SSE events."""
        if self._thread_locks[thread_id].locked():
            events.publish(thread_id, "turn_queued", {"turn_id": turn_id})
        try:
            async with self._thread_locks[thread_id]:
                await self._run_turn_locked(
                    user_id,
                    thread_id,
                    turn_id,
                    text,
                    material_ids,
                    reference_ids,
                    unresolved_codes,
                )
        except Exception as exc:
            logger.exception("turn %s failed", turn_id)
            # 失败也落库一条 assistant 消息：SSE 断流时用户也能在历史和轮询里看到
            friendly = "任务执行失败，请重试。如多次失败请联系管理员。"
            if "Payload Too Large" in str(exc) or "413" in str(exc):
                friendly = (
                    "本会话的图片材料过大或累计过多，超出了模型接口的限制。"
                    "由于模型对话会携带全部历史，这个会话已无法继续使用图片。"
                    "请新建一个会话重新上传（新上传的图片会自动压缩，不会再出现这个问题）。"
                )
            with Session(get_engine()) as session:
                session.add(
                    Message(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        role="assistant",
                        content=f"⚠️ {friendly}\n\n错误详情：{str(exc)[:300]}",
                    )
                )
                session.commit()
            events.publish(thread_id, "error", {"turn_id": turn_id, "message": friendly})

    async def _run_turn_locked(
        self,
        user_id: str,
        thread_id: str,
        turn_id: str,
        text: str,
        material_ids: list[str] | None,
        reference_ids: list[str] | None = None,
        unresolved_codes: list[str] | None = None,
    ) -> None:
        settings = self.settings
        ws = workspace.workspace_dir(settings, user_id, thread_id)

        # 等本次关联材料的预处理（ASR 转写/抽帧/解析）完成，否则转写文本进不了上下文
        deadline = time.monotonic() + _MATERIAL_WAIT_S
        while True:
            with Session(get_engine()) as session:
                thread = session.get(Thread, thread_id)
                if thread is None:
                    raise RuntimeError(f"thread {thread_id} not found")
                statement = select(Material).where(Material.thread_id == thread_id)
                if material_ids:
                    statement = statement.where(Material.id.in_(material_ids))
                materials = list(session.exec(statement).all())
            pending = [m for m in materials if m.status == MaterialStatus.processing]
            if not pending or time.monotonic() > deadline:
                if pending:
                    logger.warning(
                        "turn %s: %d material(s) still processing after %.0fs wait",
                        turn_id, len(pending), _MATERIAL_WAIT_S,
                    )
                break
            await asyncio.sleep(1)

        # 跨会话引用：取出被引用会话的材料与对话索引（只注入索引与路径，内容由模型按需读取）
        references: list[tuple[Thread, list[Material], list[Message]]] = []
        reference_images: list[str] = []
        if reference_ids:
            with Session(get_engine()) as session:
                for ref_id in reference_ids:
                    ref_thread = session.get(Thread, ref_id)
                    if ref_thread is None or ref_thread.user_id != user_id:
                        continue
                    ref_materials = list(
                        session.exec(
                            select(Material)
                            .where(Material.thread_id == ref_id)
                            .order_by(Material.created_at)
                        ).all()
                    )
                    ref_messages = list(
                        session.exec(
                            select(Message)
                            .where(Message.thread_id == ref_id)
                            .order_by(Message.created_at)
                        ).all()
                    )
                    references.append((ref_thread, ref_materials, ref_messages))
                    for material in ref_materials:
                        if (
                            material.kind == MaterialKind.image
                            and material.status == MaterialStatus.ready
                            and material.stored_path
                            and len(reference_images) < settings.reference_image_limit
                        ):
                            reference_images.append(material.stored_path)
        reference_block = context.build_reference_block(
            references,
            message_head_chars=settings.reference_message_head_chars,
            max_chars=settings.reference_index_max_chars,
        )
        reference_records = [
            {"code": t.code, "thread_id": t.id, "title": t.title} for t, _m, _msg in references
        ]

        # 拿到锁之后才落库用户消息：与上一条 assistant 回复保持正确的先后次序
        with Session(get_engine()) as session:
            session.add(
                Message(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    role="user",
                    content=text,
                    material_ids_json=json.dumps(material_ids) if material_ids else None,
                    references_json=(
                        json.dumps(reference_records, ensure_ascii=False)
                        if reference_records
                        else None
                    ),
                )
            )
            session.commit()

        runtime = await self.get_runtime(user_id)
        client = runtime.client

        with Session(get_engine()) as session:
            thread = session.get(Thread, thread_id)
            codex_thread_id = thread.codex_thread_id if thread else None
            task_type = thread.task_type if thread else "auto"
        if codex_thread_id is None:
            started_thread = await client.thread_start(
                {
                    "cwd": str(ws),
                    "developerInstructions": context.developer_instructions(task_type),
                    "serviceName": "homework-grader",
                    "ephemeral": False,
                }
            )
            codex_thread_id = started_thread.thread.id
            with Session(get_engine()) as session:
                thread = session.get(Thread, thread_id)
                thread.codex_thread_id = codex_thread_id
                session.add(thread)
                session.commit()
            runtime.live_threads.add(codex_thread_id)
        elif codex_thread_id not in runtime.live_threads:
            # The app-server process was reaped and respawned since this codex
            # thread was created; reload it from disk into the new process.
            await client.thread_resume(codex_thread_id)
            runtime.live_threads.add(codex_thread_id)

        input_items = context.build_turn_input(
            settings,
            user_id,
            thread_id,
            text,
            materials,
            reference_block=reference_block,
            reference_images=reference_images,
        )

        # 本轮开始前的两份快照：
        # 1) 结构化结果文件的状态 —— 用来判断"这一轮到底有没有写结果"（避免把上一轮的结果反复挂到新回复上）
        # 2) 工作区文件清单 —— 用来发现模型这一轮产出的文件（朗读音频、裁好的图片等）
        uploaded_paths = {m.stored_path for m in materials if m.stored_path}
        before_results = artifacts_service.result_state(ws)
        before_files = artifacts_service.snapshot(ws, exclude=uploaded_paths)

        try:
            started = await client.turn_start(codex_thread_id, input_items)
        except InvalidRequestError as exc:
            if "thread not found" not in str(exc):
                raise
            # Process died outside our tracking; reload the thread and retry once.
            runtime.live_threads.discard(codex_thread_id)
            await client.thread_resume(codex_thread_id)
            runtime.live_threads.add(codex_thread_id)
            started = await client.turn_start(codex_thread_id, input_items)
        codex_turn_id = started.turn.id
        client.register_turn_notifications(codex_turn_id)

        # 把模型输出分成两路：commentary（过程叙述）与 final_answer（最终答复）。
        # 对话界面只显示最终答复，过程收进详情页 —— 这样用户看到的是结论而不是"我接下来要做什么"。
        final_parts: list[str] = []
        process_parts: list[str] = []
        phase_by_item: dict[str, str] = {}
        delta_buffer: dict[str, str] = {}
        turn_error: str | None = None
        try:
            while True:
                notification = await client.next_turn_notification(codex_turn_id)
                if notification.method == "item/started" and isinstance(
                    notification.payload, ItemStartedNotification
                ):
                    item = notification.payload.item
                    item_id = getattr(item, "id", None)
                    phase = getattr(item, "phase", None)
                    if item_id and phase is not None:
                        phase_by_item[item_id] = getattr(phase, "value", str(phase))
                    continue
                if notification.method == "item/completed" and isinstance(
                    notification.payload, ItemCompletedNotification
                ):
                    # 整条消息完成时才带上 phase（commentary / final_answer）与完整文本，
                    # 这是唯一可靠的"分流"依据：delta 本身不带相位。
                    fields = _item_fields(notification.payload.item)
                    if fields.get("type") == "agentMessage":
                        text = str(fields.get("text") or "")
                        phase = _phase_value(fields.get("phase"))
                        if phase == "commentary":
                            process_parts.append(text)
                            events.publish(
                                thread_id, "process_completed", {"turn_id": turn_id, "text": text}
                            )
                        elif text:
                            final_parts.append(text)
                            # 最终答复在整条消息完成时一次性推给前端（内容干净，不含过程叙述）
                            events.publish(
                                thread_id, "message_delta", {"turn_id": turn_id, "delta": text}
                            )
                            phase_by_item[fields.get("id") or ""] = "final_answer"
                        delta_buffer.pop(str(fields.get("id") or ""), None)
                    continue
                if notification.method == "item/agentMessage/delta" and isinstance(
                    notification.payload, AgentMessageDeltaNotification
                ):
                    delta = notification.payload.delta
                    item_id = notification.payload.item_id
                    delta_buffer[item_id] = delta_buffer.get(item_id, "") + delta
                    # 直播期间只把增量喂给"处理中"提示，等整条消息完成再决定它是过程还是答复
                    events.publish(
                        thread_id, "process_delta", {"turn_id": turn_id, "delta": delta}
                    )
                    continue
                if notification.method == "turn/completed" and isinstance(
                    notification.payload, TurnCompletedNotification
                ):
                    if notification.payload.turn.id != codex_turn_id:
                        continue
                    status_value = notification.payload.turn.status.value
                    if status_value != "completed":
                        err = notification.payload.turn.error
                        turn_error = (
                            err.message if err is not None else f"turn ended with status {status_value}"
                        )
                    break
        finally:
            client.unregister_turn_notifications(codex_turn_id)

        if turn_error is not None:
            raise RuntimeError(turn_error)

        # 只有"本轮被写过"的结果文件才算这一轮的结果；两个都写了则 grading 优先。
        grading = None
        english = None
        if artifacts_service.result_changed(before_results, ws, "grading_result.json"):
            grading = load_grading_result(ws)
        if grading is None and artifacts_service.result_changed(
            before_results, ws, "english_result.json"
        ):
            english = load_english_result(ws)
        result_type: str | None = None
        result_json: str | None = None
        if grading is not None:
            result_type = RESULT_TYPE_GRADING
            result_json = grading.model_dump_json()
        elif english is not None:
            result_type = RESULT_TYPE_ENGLISH_PASSAGE
            result_json = english.model_dump_json()
        # 兜底：若整轮没有任何 item/completed（协议变化或异常），用累计的 delta 当最终答复
        if not final_parts and not process_parts and delta_buffer:
            final_parts = [text for text in delta_buffer.values() if text]
        if not final_parts and process_parts:
            # 极端情况：整轮只说了过程、没有最终答复 —— 别让用户看到空白
            final_parts, process_parts = process_parts, []
        assistant_text = "".join(final_parts)
        process_text = "".join(process_parts)
        # 模型这一轮如果写了"生成请求"（语音/图片），在这里执行：
        # 必须在产出物扫描之前完成，生成出来的文件才会被登记成产出物。
        async def _notify_generation(message: str) -> None:
            events.publish(thread_id, "generation_status", {"turn_id": turn_id, "message": message})

        generated = await generation.process_requests(
            ws,
            settings,
            material_files={m.filename: m.stored_path for m in materials if m.stored_path},
            notify=_notify_generation,
        )
        if generated.notices:
            assistant_text = f"{assistant_text}\n\n" + "\n".join(generated.notices)
        if unresolved_codes:
            codes = "、".join(f"#{c}" for c in unresolved_codes)
            assistant_text = (
                f"{assistant_text}\n\n⚠️ 没找到会话 {codes}"
                "（可能码写错了，或者那不是你的会话）。"
            )

        # 模型本轮产出的文件（在写消息之前先算好，便于和消息绑定）
        produced, oversized_skipped = artifacts_service.discover(
            ws,
            before_files,
            exclude=uploaded_paths,
            hard_max_bytes=settings.artifact_hard_max_bytes,
        )
        # 超上限的文件不能静默消失：直接在回复末尾写明，用户在对话和详情页都看得到
        if oversized_skipped:
            limit_gb = settings.artifact_hard_max_bytes / 1024**3
            names = "、".join(f"{p.name}（{p.stat().st_size / 1024**2:.0f} MB）" for p in oversized_skipped[:5])
            assistant_text = (
                f"{assistant_text}\n\n⚠️ 本轮还有 {len(oversized_skipped)} 个文件超过大小上限"
                f"（{limit_gb:.1f} GB）没有上传到聊天窗口：{names}。"
                "文件仍保存在会话工作区里，需要的话请让它改小后再生成。"
            )
        created_artifacts: list[dict] = []
        with Session(get_engine()) as session:
            message = Message(
                thread_id=thread_id,
                turn_id=turn_id,
                role="assistant",
                content=assistant_text,
                process_log=process_text or None,
                grading_result_json=result_json,
                result_type=result_type,
            )
            session.add(message)
            session.commit()
            message_id = message.id
            for path in produced:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                artifact = Artifact(
                    thread_id=thread_id,
                    message_id=message_id,
                    turn_id=turn_id,
                    filename=path.name,
                    stored_path=str(path),
                    kind=detect_kind(path.name).value,
                    size=size,
                    oversized=size > settings.artifact_preview_max_bytes,
                )
                session.add(artifact)
                created_artifacts.append(
                    {
                        "id": artifact.id,
                        "filename": artifact.filename,
                        "kind": artifact.kind,
                        "size": artifact.size,
                        "oversized": artifact.oversized,
                    }
                )
            if produced:
                session.commit()
            # 图片/视频在后台做"浏览器友好"的预览副本（缩放/转码），完成后通过 SSE 通知前端
            for item in created_artifacts:
                if item["kind"] in ("image", "video"):
                    asyncio.create_task(media_prep.prepare_artifact(item["id"]))
            if grading is not None:
                # 自动收录答错的题到错题本（按 source_message_id + question 去重）
                record_mistakes(
                    session,
                    user_id=user_id,
                    thread_id=thread_id,
                    message_id=message_id,
                    grading=grading,
                )

        events.publish(
            thread_id,
            "turn_completed",
            {
                "turn_id": turn_id,
                "message_id": message_id,
                "result_type": result_type,
                "grading_result": (json.loads(result_json) if result_json else None),
                "artifacts": created_artifacts,
                # 最终答复与过程分开给前端：实时流里只有最终答复，过程单独展示
                "text": assistant_text,
                "process_log": process_text,
            },
        )


_manager: CodexManager | None = None


def get_manager() -> CodexManager:
    global _manager
    if _manager is None:
        _manager = CodexManager()
    return _manager


def reset_manager(manager: CodexManager | None) -> None:
    """Test hook."""
    global _manager
    _manager = manager
