"""Per-user / per-thread filesystem layout for codex workspaces."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import Settings


def user_dir(settings: Settings, user_id: str) -> Path:
    return settings.users_dir / user_id


def codex_home(settings: Settings, user_id: str) -> Path:
    return user_dir(settings, user_id) / "codex_home"


def thread_dir(settings: Settings, user_id: str, thread_id: str) -> Path:
    return user_dir(settings, user_id) / "threads" / thread_id


def workspace_dir(settings: Settings, user_id: str, thread_id: str) -> Path:
    path = thread_dir(settings, user_id, thread_id) / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def keyframes_dir(settings: Settings, user_id: str, thread_id: str) -> Path:
    path = workspace_dir(settings, user_id, thread_id) / "keyframes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def keyframes_for_material(settings: Settings, user_id: str, thread_id: str, material_id: str) -> list[Path]:
    directory = workspace_dir(settings, user_id, thread_id) / "keyframes"
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{material_id}_*.jpg"))


CONFIG_TOML_TEMPLATE = """model = "deepseek-flash"
model_provider = "deepseek"
preferred_auth_method = "apikey"
forced_login_method = "api"
web_search = "disabled"
model_catalog_json = "{models_json}"
disable_response_storage = true
sandbox_mode = "workspace-write"

[model_providers.deepseek]
name = "deepseek"
base_url = "https://api.deepseek.com/"
wire_api = "responses"
experimental_bearer_token = "{token}"

# DeepSeek 没有 OpenAI 托管的 web_search 工具，联网搜索通过 MCP 提供
# 版本必须锁定：uvx 默认拉最新版，而 MCP server 是 codex 拉起的普通子进程、
# 不受沙箱约束（= 服务账号的全部文件权限 + 网络出口），未锁版本等于把供应链风险直接放进来。
# 升级时改这里的版本号并重启服务即可（存量用户的 config.toml 会自动重写）。
[mcp_servers.duckduckgo]
command = "{uvx_bin}"
args = ["duckduckgo-mcp-server==0.7.0"]
default_tools_approval_mode = "approve"

[projects."{user_dir}"]
trust_level = "trusted"
"""


def ensure_user_codex_home(settings: Settings, user_id: str) -> Path:
    """Create the per-user CODEX_HOME with a deepseek config and model catalog.

    The bearer token is injected from the backend process environment and the
    resulting file is chmod 600. The token is never logged.
    """
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set in the backend environment")
    home = codex_home(settings, user_id)
    home.mkdir(parents=True, exist_ok=True)

    models_target = home / "models.json"
    if not models_target.exists():
        models_target.write_bytes(Path(settings.source_models_json).read_bytes())

    config_path = home / "config.toml"
    content = CONFIG_TOML_TEMPLATE.format(
        models_json=models_target,
        token=settings.deepseek_api_key,
        user_dir=user_dir(settings, user_id),
        uvx_bin=shutil.which("uvx") or "uvx",
    )
    if not config_path.exists() or config_path.read_text() != content:
        config_path.write_text(content)
    config_path.chmod(0o600)
    return home
