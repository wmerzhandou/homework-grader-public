"""SQLModel table definitions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    """序列化为带 UTC 标记的 ISO 字符串。

    SQLite 存取会丢掉 tzinfo，补回时区标记后浏览器才能正确转换为本地时间。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex


class MaterialKind(str, Enum):
    document = "document"
    image = "image"
    audio = "audio"
    video = "video"


class MaterialStatus(str, Enum):
    processing = "processing"
    ready = "ready"
    failed = "failed"


class User(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    is_admin: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


class InviteCode(SQLModel, table=True):
    code: str = Field(primary_key=True)
    used_by: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class AuthToken(SQLModel, table=True):
    token: str = Field(primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    # 过期时间；NULL 表示历史遗留 token（迁移脚本会回填 created_at + TTL）
    expires_at: datetime | None = Field(default=None, index=True)


# 合法的任务类型：auto（自动判断）/ grading（批改作业）/ english_passage（英语短文）/ general（普通问答）
TASK_TYPES = ("auto", "grading", "english_passage", "general")
DEFAULT_TASK_TYPE = "auto"

# Message.result_type 的取值：grading_result.json → grading，english_result.json → english_passage
RESULT_TYPE_GRADING = "grading"
RESULT_TYPE_ENGLISH_PASSAGE = "english_passage"


class Thread(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    title: str = "未命名作业"
    # 会话码：前端展示、方便引用（4 位、排除易混字符），同一用户内唯一
    code: str = Field(default="", index=True)
    task_type: str = DEFAULT_TASK_TYPE
    codex_thread_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class Material(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    thread_id: str = Field(foreign_key="thread.id", index=True)
    filename: str
    stored_path: str
    kind: MaterialKind
    status: MaterialStatus = MaterialStatus.processing
    purpose: str = ""
    summary: str = ""
    transcript: str = ""
    # 预处理产出的"可读文本"落盘路径（文档→markdown、音视频→转写），供按需读取
    text_path: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class Artifact(SQLModel, table=True):
    """会话产出物：模型在某一轮里自己写进工作区的文件（朗读音频、裁好的图片等）。

    与 Material 的区别：Material 是用户上传的原文件（输入），Artifact 是模型产出的（输出）。
    只有登记成 Artifact 的文件才有下载/播放入口，否则前端只能看到模型写在正文里的服务器路径。
    """

    id: str = Field(default_factory=_uuid, primary_key=True)
    thread_id: str = Field(foreign_key="thread.id", index=True)
    message_id: str | None = Field(default=None, index=True)
    turn_id: str | None = None
    filename: str
    stored_path: str
    kind: str = "document"
    size: int = 0
    # 浏览器友好的预览副本（转码/缩放后的文件）；为空表示没有，直接用原文件预览
    preview_path: str | None = None
    # 文件过大：只在界面给"下载"入口，不做内嵌播放/预览
    oversized: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


class Message(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    thread_id: str = Field(foreign_key="thread.id", index=True)
    turn_id: str | None = Field(default=None, index=True)
    role: str  # "user" | "assistant"
    content: str = ""
    # 模型的过程叙述（commentary 阶段）：只在详情页折叠展示，不进对话流
    process_log: str | None = None
    grading_result_json: str | None = None
    result_type: str | None = None  # "grading" | "english_passage"，对应本轮产生的结果文件类型
    material_ids_json: str | None = None  # JSON array of Material ids attached to this message
    # 本条消息引用了哪些会话：[{"code","thread_id","title"}]
    references_json: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class Mistake(SQLModel, table=True):
    """错题本：批改结果中答错的题目自动收录，可标记"已掌握"。"""

    id: str = Field(default_factory=_uuid, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    knowledge_point: str = ""
    question: str
    student_answer: str = ""
    correct_answer: str = ""
    error_reason: str = ""
    explanation: str = ""
    source_thread_id: str | None = None
    source_message_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    mastered: bool = False


class AccessLog(SQLModel, table=True):
    """访问记录：谁（IP/设备）在什么时候访问了哪些路径。

    只记录有意义的请求（页面加载与 /api/*），跳过静态资源、健康检查、本机回环
    和管理页自身的轮询，避免日志表被噪声淹没。查询串一律不记（避免 ?ticket= 凭据入库）。
    """

    id: int | None = Field(default=None, primary_key=True)
    ip: str = Field(index=True)
    method: str
    path: str
    status: int
    user_id: str | None = Field(default=None, index=True)
    user_agent: str = ""
    # 前端生成的随机设备标识（X-Client-Id），用于"同一设备换 IP"的关联
    device_id: str = Field(default="", index=True)
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class IpGeo(SQLModel, table=True):
    """IP 归属缓存：离线库查询一次后落库，避免重复查询、也让历史数据稳定。"""

    ip: str = Field(primary_key=True)
    country: str = ""
    province: str = ""
    city: str = ""
    asn: int | None = None
    org: str = ""
    network_type: str = ""
    resolved_at: datetime = Field(default_factory=_utcnow)
    # 由管理员标记的"可信来源"（自家网络/公司出口），打分时会降权
    trusted: bool = False
    note: str = ""


class Device(SQLModel, table=True):
    """设备：由前端随机 ID 标识，可标记为可信（家人设备）。"""

    id: str = Field(primary_key=True)
    label: str = ""
    user_agent: str = ""
    first_seen: datetime = Field(default_factory=_utcnow)
    last_seen: datetime = Field(default_factory=_utcnow, index=True)
    last_ip: str = ""
    trusted: bool = False
    note: str = ""


class IpBlock(SQLModel, table=True):
    """IP 封禁名单。expires_at 为空表示永久封禁。"""

    ip: str = Field(primary_key=True)
    reason: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime | None = None
