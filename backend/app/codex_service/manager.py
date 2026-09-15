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
from sqlmodel import Session, select

from .. import events
from ..config import Settings, get_settings
from ..db import get_engine
from ..mistakes import record_mistakes
from ..models import (
    RESULT_TYPE_ENGLISH_PASSAGE,
    RESULT_TYPE_GRADING,
    Material,
    MaterialStatus,
    Message,
    Thread,
)
from . import context, workspace
from .grading import load_english_result, load_grading_result

logger = logging.getLogger(__name__)

_REAPER_INTERVAL_S = 60.0
# 等待材料预处理完成的最长时间（秒）
_MATERIAL_WAIT_S = 90.0


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
    ) -> None:
        """Execute one codex turn in the background, streaming SSE events."""
        if self._thread_locks[thread_id].locked():
            events.publish(thread_id, "turn_queued", {"turn_id": turn_id})
        try:
            async with self._thread_locks[thread_id]:
                await self._run_turn_locked(user_id, thread_id, turn_id, text, material_ids)
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

        # 拿到锁之后才落库用户消息：与上一条 assistant 回复保持正确的先后次序
        with Session(get_engine()) as session:
            session.add(
                Message(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    role="user",
                    content=text,
                    material_ids_json=json.dumps(material_ids) if material_ids else None,
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

        input_items = context.build_turn_input(settings, user_id, thread_id, text, materials)

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

        accumulated: list[str] = []
        turn_error: str | None = None
        try:
            while True:
                notification = await client.next_turn_notification(codex_turn_id)
                if notification.method == "item/agentMessage/delta" and isinstance(
                    notification.payload, AgentMessageDeltaNotification
                ):
                    delta = notification.payload.delta
                    accumulated.append(delta)
                    events.publish(
                        thread_id, "message_delta", {"turn_id": turn_id, "delta": delta}
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

        grading = load_grading_result(ws)
        # 依次检查两种结果文件：grading_result.json 优先，其次 english_result.json
        result_type: str | None = None
        result_json: str | None = None
        if grading is not None:
            result_type = RESULT_TYPE_GRADING
            result_json = grading.model_dump_json()
        else:
            english = load_english_result(ws)
            if english is not None:
                result_type = RESULT_TYPE_ENGLISH_PASSAGE
                result_json = english.model_dump_json()
        assistant_text = "".join(accumulated)
        with Session(get_engine()) as session:
            message = Message(
                thread_id=thread_id,
                turn_id=turn_id,
                role="assistant",
                content=assistant_text,
                grading_result_json=result_json,
                result_type=result_type,
            )
            session.add(message)
            session.commit()
            message_id = message.id
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
