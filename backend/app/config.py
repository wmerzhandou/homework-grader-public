"""Application settings, sourced from environment variables."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_ROOT.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    asr_base_url: str
    tts_base_url: str
    admin_password: str
    deepseek_api_key: str | None
    codex_bin: str
    codex_idle_timeout_s: float
    ffmpeg_bin: str
    source_models_json: Path
    # ---- 安全相关 ----
    # 是否暴露 /docs /redoc /openapi.json（默认关闭，只在开发时显式打开）
    api_docs_enabled: bool
    # 允许的跨域来源；留空表示不启用 CORS（生产是同源部署，不需要）
    cors_allow_origins: tuple[str, ...]
    # 登录 token 有效期（秒）
    auth_token_ttl_s: int
    # 材料下载短时票据有效期（秒）
    media_ticket_ttl_s: int
    # 票据签名密钥；留空则由 security.ensure_app_secret 在 data_dir 下生成并落盘
    app_secret: str | None
    # 单次上传的文件个数 / 总字节数上限
    max_upload_files: int
    max_upload_total_bytes: int
    # ---- 讲解旁白的默认音色（换音色只改这两个环境变量，不用动代码）----
    # engine: index_tts（本机，免费）/ mmx（MiniMax 云端音色库）
    narration_engine: str
    # voice: index_tts 固定音色写 default；mmx 用音色名，如 "female-chengshu"
    narration_voice: str
    # 讲解旁白的默认语速（<1 更慢；不同音色天生的语速不同，换音色时一起调它）
    narration_speed: float
    # 讲解旁白的目标响度（LUFS）。不同音色/引擎的原始音量差很多（实测同一句话
    # 差过 9dB），统一到同一个目标，换音色时音量才不会忽大忽小；填 0 关闭
    narration_loudness_lufs: float
    # Manim（数学动画）CLI 与超时：装在独立 venv 里，不用后端 venv 的包
    manim_bin: str
    manim_timeout_s: float
    # 默认出片高度：讲解/数学动画默认压到 720p（够看、文件小、渲染快）；
    # 用户明确要"高清/1080p"（请求里 quality=high）时不压
    video_max_height: int
    # codex 会话历史超过这个大小（MB）就换一个新会话（防止请求体顶到 413）
    codex_session_max_mb: float
    # 每用户材料总占用上限（字节）
    user_storage_quota_bytes: int
    # 登录/注册限流：窗口期内允许的尝试次数与窗口秒数
    auth_rate_limit_attempts: int
    auth_rate_limit_window_s: float
    # 访问记录保留天数（管理页的监控数据）
    access_log_retention_days: int
    # 离线 IP 归属库（GeoLite2-City / GeoLite2-ASN），放在 DATA_DIR/geoip 下
    geoip_city_db: Path
    geoip_asn_db: Path
    # 产出物：超过 preview 阈值就不做内嵌预览，只给下载；超过 hard 上限则不登记（并明确告知）
    artifact_preview_max_bytes: int
    artifact_hard_max_bytes: int
    # 图片预览缩放的最长边
    artifact_image_max_dim: int
    # 生成能力：mmx CLI 路径（systemd 的 PATH 里没有 ~/.local/bin，所以显式配置）
    mmx_bin: str
    mmx_timeout_s: float
    tts_generation_timeout_s: float
    # 视频生成（H3 农场 / ComfyUI）
    comfyui_base_url: str
    h3_t2v_workflow: Path
    h3_r2v_workflow: Path
    video_generation_timeout_s: float
    # 讲解视频渲染（HyperFrames：HTML 组合 → mp4）
    hyperframes_bin: str
    hf_template_dir: Path
    render_timeout_s: float
    # OpenMontage 技能库（精选技能软链进 CODEX_HOME/skills，整库软链成 skill-library 供按需查阅）
    skill_library_dir: Path
    # 跨会话引用：注入对话索引的每条消息取多少字、整块最多多少字、图片最多带几张
    reference_message_head_chars: int
    reference_index_max_chars: int
    reference_image_limit: int

    @property
    def users_dir(self) -> Path:
        return self.data_dir / "users"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _settings_from_env() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", PROJECT_ROOT / "data")).resolve()
    cors_origins = tuple(
        origin.strip()
        for origin in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
        if origin.strip()
    )
    return Settings(
        data_dir=data_dir,
        database_url=os.environ.get("DATABASE_URL", f"sqlite:///{data_dir / 'app.db'}"),
        asr_base_url=os.environ.get("ASR_BASE_URL", "http://127.0.0.1:8020").rstrip("/"),
        tts_base_url=os.environ.get("TTS_BASE_URL", "http://127.0.0.1:5000").rstrip("/"),
        admin_password=os.environ.get("ADMIN_PASSWORD", "admin123"),
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
        codex_bin=os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex",
        codex_idle_timeout_s=float(os.environ.get("CODEX_IDLE_TIMEOUT_S", 30 * 60)),
        ffmpeg_bin=os.environ.get("FFMPEG_BIN", "ffmpeg"),
        source_models_json=Path(
            os.environ.get("CODEX_MODELS_JSON", Path.home() / ".codex" / "models.json")
        ),
        api_docs_enabled=_env_bool("API_DOCS_ENABLED", False),
        cors_allow_origins=cors_origins,
        # 默认 7 天：手机随时可能丢/借给孩子，缩短窗口比"永不失效"稳妥得多
        auth_token_ttl_s=int(os.environ.get("AUTH_TOKEN_TTL_S", 7 * 24 * 3600)),
        media_ticket_ttl_s=int(os.environ.get("MEDIA_TICKET_TTL_S", 15 * 60)),
        app_secret=os.environ.get("APP_SECRET") or None,
        max_upload_files=int(os.environ.get("MAX_UPLOAD_FILES", 10)),
        max_upload_total_bytes=int(os.environ.get("MAX_UPLOAD_TOTAL_BYTES", 300 * 1024 * 1024)),
        user_storage_quota_bytes=int(
            os.environ.get("USER_STORAGE_QUOTA_BYTES", 5 * 1024 * 1024 * 1024)
        ),
        auth_rate_limit_attempts=int(os.environ.get("AUTH_RATE_LIMIT_ATTEMPTS", 10)),
        auth_rate_limit_window_s=float(os.environ.get("AUTH_RATE_LIMIT_WINDOW_S", 300)),
        access_log_retention_days=int(os.environ.get("ACCESS_LOG_RETENTION_DAYS", 90)),
        geoip_city_db=Path(
            os.environ.get("GEOIP_CITY_DB", data_dir / "geoip" / "GeoLite2-City.mmdb")
        ),
        geoip_asn_db=Path(
            os.environ.get("GEOIP_ASN_DB", data_dir / "geoip" / "GeoLite2-ASN.mmdb")
        ),
        artifact_preview_max_bytes=int(
            os.environ.get("ARTIFACT_PREVIEW_MAX_BYTES", 40 * 1024 * 1024)
        ),
        artifact_hard_max_bytes=int(
            os.environ.get("ARTIFACT_HARD_MAX_BYTES", 1024 * 1024 * 1024)
        ),
        artifact_image_max_dim=int(os.environ.get("ARTIFACT_IMAGE_MAX_DIM", 2000)),
        mmx_bin=os.environ.get("MMX_BIN")
        or shutil.which("mmx")
        or str(Path.home() / ".local" / "bin" / "mmx"),
        mmx_timeout_s=float(os.environ.get("MMX_TIMEOUT_S", 300)),
        narration_engine=os.environ.get("NARRATION_ENGINE", "index_tts").strip() or "index_tts",
        narration_voice=os.environ.get("NARRATION_VOICE", "default").strip() or "default",
        narration_speed=float(os.environ.get("NARRATION_SPEED", 0.85)),
        narration_loudness_lufs=float(os.environ.get("NARRATION_LOUDNESS_LUFS", -16)),
        manim_bin=os.environ.get("MANIM_BIN")
        or shutil.which("manim")
        or str(Path.home() / ".local" / "venvs" / "manim" / "bin" / "manim"),
        manim_timeout_s=float(os.environ.get("MANIM_TIMEOUT_S", 1800)),
        video_max_height=int(os.environ.get("VIDEO_MAX_HEIGHT", 720)),
        codex_session_max_mb=float(os.environ.get("CODEX_SESSION_MAX_MB", 12)),
        tts_generation_timeout_s=float(os.environ.get("TTS_GENERATION_TIMEOUT_S", 300)),
        comfyui_base_url=os.environ.get(
            "COMFYUI_SERVER_URL", "http://127.0.0.1:8188"
        ).rstrip("/"),
        h3_t2v_workflow=Path(
            os.environ.get(
                "H3_T2V_WORKFLOW", "/opt/openmontage/assets/workflows/h3_t2v_api.json"
            )
        ),
        h3_r2v_workflow=Path(
            os.environ.get(
                "H3_R2V_WORKFLOW", "/opt/openmontage/assets/workflows/h3_r2v_api.json"
            )
        ),
        video_generation_timeout_s=float(os.environ.get("VIDEO_GENERATION_TIMEOUT_S", 1200)),
        hyperframes_bin=os.environ.get("HYPERFRAMES_BIN")
        or shutil.which("hyperframes")
        or str(Path.home() / ".local" / "bin" / "hyperframes"),
        hf_template_dir=Path(
            os.environ.get(
                "HF_TEMPLATE_DIR", str(BACKEND_ROOT / "app" / "render_template")
            )
        ),
        render_timeout_s=float(os.environ.get("RENDER_TIMEOUT_S", 1800)),
        skill_library_dir=Path(
            os.environ.get("SKILL_LIBRARY_DIR", "/home/user/.agents/skills")
        ),
        reference_message_head_chars=int(os.environ.get("REFERENCE_MESSAGE_HEAD_CHARS", 120)),
        reference_index_max_chars=int(os.environ.get("REFERENCE_INDEX_MAX_CHARS", 6000)),
        reference_image_limit=int(os.environ.get("REFERENCE_IMAGE_LIMIT", 3)),
    )


@lru_cache
def get_settings() -> Settings:
    return _settings_from_env()
