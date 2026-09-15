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
    # 每用户材料总占用上限（字节）
    user_storage_quota_bytes: int
    # 登录/注册限流：窗口期内允许的尝试次数与窗口秒数
    auth_rate_limit_attempts: int
    auth_rate_limit_window_s: float

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
        auth_token_ttl_s=int(os.environ.get("AUTH_TOKEN_TTL_S", 30 * 24 * 3600)),
        media_ticket_ttl_s=int(os.environ.get("MEDIA_TICKET_TTL_S", 15 * 60)),
        app_secret=os.environ.get("APP_SECRET") or None,
        max_upload_files=int(os.environ.get("MAX_UPLOAD_FILES", 10)),
        max_upload_total_bytes=int(os.environ.get("MAX_UPLOAD_TOTAL_BYTES", 300 * 1024 * 1024)),
        user_storage_quota_bytes=int(
            os.environ.get("USER_STORAGE_QUOTA_BYTES", 5 * 1024 * 1024 * 1024)
        ),
        auth_rate_limit_attempts=int(os.environ.get("AUTH_RATE_LIMIT_ATTEMPTS", 10)),
        auth_rate_limit_window_s=float(os.environ.get("AUTH_RATE_LIMIT_WINDOW_S", 300)),
    )


@lru_cache
def get_settings() -> Settings:
    return _settings_from_env()
