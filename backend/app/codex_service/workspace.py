"""Per-user / per-thread filesystem layout for codex workspaces."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import Settings


def user_dir(settings: Settings, user_id: str) -> Path:
    return settings.users_dir / user_id


def codex_home(settings: Settings, user_id: str) -> Path:
    return user_dir(settings, user_id) / "codex_home"


# 技能来自 OpenMontage 的技能库（机器级那份是全量的；studio 副本会少几个）。
DEFAULT_SKILL_LIBRARY = Path("/home/user/.agents/skills")
# 精选注册：只挂"讲解/动画/图解/数学"这条线上真用得上的，
# 避免 100 个技能的名字+描述每轮都进提示词（约 37KB）并把路由搅浑。
LINKED_SKILLS = (
    # HyperFrames 全家桶（讲解视频主力）
    "hyperframes",
    "hyperframes-core",
    "hyperframes-animation",
    "hyperframes-creative",
    "hyperframes-cli",
    "hyperframes-keyframes",
    "hyperframes-audio",
    "hyperframes-registry",
    # 图解 / 数学可视化
    "beautiful-mermaid",
    "d3-viz",
    "manim-composer",
    "manimce-best-practices",
    # 动画基础
    "gsap-core",
    "gsap-timeline",
    "svg-character-animation",
    "canvas-procedural-animation",
    # 工程与素材
    "ffmpeg",
    "video-understand",
    "visual-style",
    "motion-graphics",
)
LOCAL_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _tree_signature(root: Path) -> dict[str, tuple[int, int]]:
    """目录签名：相对路径 → (大小, mtime)。用来判断自研技能是否需要重新拷贝。"""
    signature: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return signature
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        stat = path.stat()
        signature[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return signature


def _sync_local_skill(source: Path, target: Path) -> None:
    """自研技能跟着代码走：内容变了就整体替换（否则用户机器上会一直用旧版提示词）。"""
    if target.exists() and _tree_signature(source) == _tree_signature(target):
        return
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    try:
        shutil.copytree(source, target)
    except OSError:
        pass


def ensure_skills(home: Path, library: Path | None = None) -> None:
    """挂技能：精选链接 + 整库只读可查 + 自带技能。

    - 精选技能软链到 CODEX_HOME/skills（codex 从这里发现技能，名字+描述会进提示词）；
    - 整库软链成 CODEX_HOME/skill-library（不注册，模型需要时按路径读，零提示词成本）；
    - 自研技能从仓库 app/skills 拷贝进来（它们是"入口契约"，要跟着代码走）。
    幂等：链接指向不对会重链（技能库换位置时能自动纠正）。
    """
    source_root = Path(library) if library else DEFAULT_SKILL_LIBRARY
    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for name in LINKED_SKILLS:
        source = source_root / name
        target = skills_dir / name
        if not source.is_dir():
            continue
        if target.is_symlink():
            if target.resolve() == source.resolve():
                continue
            target.unlink()  # 指向旧库（例如 studio 副本）时重链
        elif target.exists():
            continue
        try:
            target.symlink_to(source)
        except OSError:
            continue
    # 整库只读可查：不进提示词，模型需要深挖时自己读
    library_link = home / "skill-library"
    if source_root.is_dir():
        if library_link.is_symlink() and library_link.resolve() != source_root.resolve():
            library_link.unlink()
        if not library_link.exists():
            try:
                library_link.symlink_to(source_root)
            except OSError:
                pass
    if LOCAL_SKILLS_DIR.is_dir():
        for source in LOCAL_SKILLS_DIR.iterdir():
            if source.is_dir():
                _sync_local_skill(source, skills_dir / source.name)


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
    ensure_skills(home, settings.skill_library_dir)

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
