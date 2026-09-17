"""会话产出物的发现：找出"模型这一轮写进工作区的文件"。

为什么要做这件事：模型可以生成朗读音频、裁好的图片等（用户明确要过这类东西），
但这些文件既不是用户上传的 Material，也没有下载路由，前端只能看到模型写在正文里的
服务器绝对路径 —— 点开必然 404。这里负责把它们识别出来，交给 manager 登记成 Artifact。

识别规则：工作区里**本轮新增或被修改**的文件，且不在排除名单里
（用户上传的原文件、keyframes/ 抽帧、grading_result.json / english_result.json、
 以及 .git/.codex/.agents 这类工具目录）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# 结果文件由结构化结果通道处理，不走产出物
RESULT_FILENAMES = ("grading_result.json", "english_result.json")
# 工具/中间目录，以及抽帧、切片、缓存目录
IGNORED_DIR_NAMES = {
    ".git", ".codex", ".agents", "keyframes", "__pycache__", "node_modules",
    "tmp", "temp", ".tmp", "cache", "frames", "segments", "chunks", "work",
}
IGNORED_FILE_NAMES = {*RESULT_FILENAMES, ".DS_Store", "Thumbs.db"}
# 渲染脚手架自带的东西：模型从模板拷进工作区的依赖/清单，不是它能交付给用户的产出物
# （`assets/gsap.min.js` 是渲染模板里的本地 GSAP；`assets/narration.*` 是后端补的音轨）
IGNORED_REL_PATHS = {"assets/gsap.min.js", "hyperframes.json", "meta.json", "composition-skeleton.html"}
IGNORED_REL_PREFIXES = ("assets/narration.",)

# 逐帧产物的典型命名：frame_0001.png / 0007.jpg / seg03.mp4 …这些属于中间产物，不该进聊天窗口
_INTERMEDIATE_RE = re.compile(
    r"^(frame|frames|f|img|image|out|output|segment|seg|chunk|shot)[-_]?\d{2,}\.(png|jpe?g|webp|mp4|mov)$"
    r"|^\d{4,}\.(png|jpe?g|webp)$",
    re.IGNORECASE,
)


def is_ignored(rel_path: Path) -> bool:
    """相对工作区的路径是否应当忽略。"""
    if rel_path.name in IGNORED_FILE_NAMES:
        return True
    if _INTERMEDIATE_RE.match(rel_path.name):
        return True
    posix = rel_path.as_posix()
    if posix in IGNORED_REL_PATHS or posix.startswith(IGNORED_REL_PREFIXES):
        return True
    for part in rel_path.parts:
        if part in IGNORED_DIR_NAMES or part.startswith("."):
            return True
    return False


def snapshot(workspace: Path, *, exclude: set[str] | None = None) -> dict[str, tuple[int, int]]:
    """工作区文件快照：相对路径 → (mtime_ns, size)。exclude 里放用户上传原文件的绝对路径。"""
    excluded = {str(Path(p).resolve()) for p in (exclude or set()) if p}
    state: dict[str, tuple[int, int]] = {}
    if not workspace.is_dir():
        return state
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace)
        if is_ignored(rel):
            continue
        if str(path.resolve()) in excluded:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        state[rel.as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return state


def discover(
    workspace: Path,
    before: dict[str, tuple[int, int]],
    *,
    exclude: set[str] | None = None,
    hard_max_bytes: int | None = None,
) -> tuple[list[Path], list[Path]]:
    """对比快照，返回 (登记的产出物, 因为太大被跳过的文件)。

    **不再静默丢弃**：超过上限的文件会作为第二个返回值交给调用方，由它给用户一条明确提示。
    """
    after = snapshot(workspace, exclude=exclude)
    changed: list[Path] = []
    skipped: list[Path] = []
    for rel, state in after.items():
        if before.get(rel) == state:
            continue
        path = workspace / rel
        try:
            if hard_max_bytes is not None and path.stat().st_size > hard_max_bytes:
                skipped.append(path)
                continue
        except OSError:
            continue
        changed.append(path)
    changed.sort(key=lambda p: p.stat().st_mtime_ns)
    return changed, skipped


def _fingerprint(path: Path) -> tuple[int, int, str] | None:
    if not path.is_file():
        return None
    try:
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16] if stat.st_size < 5_000_000 else ""
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, digest)


def result_state(workspace: Path) -> dict[str, tuple[int, int, str] | None]:
    """记录两个结构化结果文件在本轮开始前的状态。"""
    return {name: _fingerprint(workspace / name) for name in RESULT_FILENAMES}


def result_changed(before: dict[str, tuple[int, int, str] | None], workspace: Path, name: str) -> bool:
    """本轮该结果文件是否被写过（新增、修改都算）。"""
    return before.get(name) != _fingerprint(workspace / name)
