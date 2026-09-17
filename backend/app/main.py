"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from .api import auth as auth_api
from .api import admin as admin_api
from .api import events as events_api
from .api import materials as materials_api
from .api import mistakes as mistakes_api
from .api import threads as threads_api
from .api import tts as tts_api
from .codex_service.manager import get_manager
from .config import PROJECT_ROOT, get_settings
from .db import engine_db_path, get_engine, init_db
from .access import AccessControlMiddleware, prune_access_log
from .security import (
    SecurityHeadersMiddleware,
    harden_access_log,
    harden_data_dir,
    restrict_path,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"

# uvicorn 默认把完整 request line（含 ?ticket=/?token= 查询串）写进访问日志，
# 装个过滤器把它脱敏掉，避免凭据落进 journald。
harden_access_log()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    # data/ 里是口令哈希、token、材料原文件：收紧到 750，库文件 600
    harden_data_dir(settings.data_dir, engine_db_path())
    with Session(get_engine()) as session:
        removed = prune_access_log(session, settings.access_log_retention_days)
    if removed:
        logger.info("已清理 %d 条超过 %d 天的访问记录", removed, settings.access_log_retention_days)
    with Session(get_engine()) as session:
        codes = auth_api.seed_admin(session, settings.admin_password)
    if codes:
        codes_file = settings.data_dir / "invite_codes.txt"
        try:
            with codes_file.open("a", encoding="utf-8") as fh:
                fh.writelines(f"{code}\n" for code in codes)
            restrict_path(codes_file, 0o600)
            logger.info("已生成 %d 个新邀请码，见 %s", len(codes), codes_file)
        except OSError as exc:  # pragma: no cover - 依赖运行环境
            logger.warning("写入邀请码文件失败：%s", exc)
    manager = get_manager()
    manager.start_reaper()
    app.state.codex_manager = manager
    yield
    await manager.shutdown()


_settings = get_settings()

# 生产是同源部署（后端直接托管 dist），默认不开放 API 文档与跨域；
# 需要调试时用 API_DOCS_ENABLED=true / CORS_ALLOW_ORIGINS 显式打开。
app = FastAPI(
    title="homework-grader",
    lifespan=lifespan,
    docs_url="/docs" if _settings.api_docs_enabled else None,
    redoc_url="/redoc" if _settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if _settings.api_docs_enabled else None,
)

if _settings.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_settings.cors_allow_origins),
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
        max_age=600,
    )

# 中间件顺序（后加的更靠外）：安全头 → 访问控制（封禁/记录）→ CORS → 路由
app.add_middleware(AccessControlMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

if not _settings.api_docs_enabled:
    # 文档关闭时显式 404，避免落到 SPA 回退返回 200 的 index.html 造成误解
    @app.get("/docs", include_in_schema=False)
    @app.get("/redoc", include_in_schema=False)
    @app.get("/openapi.json", include_in_schema=False)
    def docs_disabled() -> None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

app.include_router(auth_api.router)
app.include_router(admin_api.router)
app.include_router(threads_api.router)
app.include_router(materials_api.router)
app.include_router(mistakes_api.router)
app.include_router(events_api.router)
app.include_router(tts_api.router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# 生产模式：后端直接托管前端构建产物（SPA），告别 vite dev server 的 HMR 问题
if FRONTEND_DIST.is_dir():
    app.mount(
        "/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets"
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        """非 /api 路径一律回退到 index.html，由前端路由接管。"""
        candidate = (FRONTEND_DIST / full_path).resolve()
        # 1) 真实存在的静态文件（/favicon.svg、/icons.svg 等）优先返回
        if (
            full_path
            and candidate.is_file()
            and candidate.is_relative_to(FRONTEND_DIST.resolve())
        ):
            return FileResponse(candidate)
        # 2) 以下情况不回退到 index.html，一律 404 JSON：
        #    - /api/* ：否则 GET 一个 POST-only 接口会拿到 200 + HTML，掩盖真实错误
        #    - 含 . 开头的路径段（/.env、/.git/config）：避免扫描器误读成"文件被暴露"
        #    - 末段带扩展名（/data/app.db、/backend/app/main.py）：看起来像要文件，不该给首页
        segments = full_path.split("/")
        looks_like_file = "." in segments[-1] if segments else False
        if (
            full_path.startswith("api/")
            or any(part.startswith(".") for part in segments)
            or looks_like_file
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        return FileResponse(FRONTEND_DIST / "index.html")
