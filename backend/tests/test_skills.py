"""技能挂载：精选注册 + 整库只读可查 + 换库自动重链。"""

from __future__ import annotations

from pathlib import Path

from app.codex_service import workspace


def _make_library(root: Path, names: list[str]) -> Path:
    library = root / "library"
    for name in names:
        skill = library / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return library


def test_ensure_skills_links_curated_set_and_library(tmp_path):
    library = _make_library(tmp_path, list(workspace.LINKED_SKILLS) + ["extra-skill"])
    home = tmp_path / "codex_home"
    home.mkdir()

    workspace.ensure_skills(home, library)

    skills = sorted(p.name for p in (home / "skills").iterdir())
    # 精选技能都挂上了
    for name in workspace.LINKED_SKILLS:
        assert name in skills, f"{name} 没挂上"
    # 库里没注册的不会进 skills，但能通过整库软链查到
    assert "extra-skill" not in skills
    library_link = home / "skill-library"
    assert library_link.is_symlink() and library_link.resolve() == library.resolve()
    assert (library_link / "extra-skill" / "SKILL.md").is_file()
    # 自带技能是拷贝（跟着代码走），不是软链
    own = home / "skills" / "explainer-video"
    assert own.is_dir() and not own.is_symlink()


def test_ensure_skills_relinks_when_library_changes(tmp_path):
    """技能库换位置（例如从 studio 副本切到全量库）时，旧链接要被纠正。"""
    old_library = _make_library(tmp_path / "old", ["hyperframes-core"])
    new_library = _make_library(tmp_path / "new", ["hyperframes-core"])
    home = tmp_path / "codex_home"
    home.mkdir()

    workspace.ensure_skills(home, old_library)
    assert (home / "skills" / "hyperframes-core").resolve() == (old_library / "hyperframes-core").resolve()

    workspace.ensure_skills(home, new_library)
    assert (home / "skills" / "hyperframes-core").resolve() == (new_library / "hyperframes-core").resolve()
    assert (home / "skill-library").resolve() == new_library.resolve()


def test_ensure_skills_is_idempotent(tmp_path):
    library = _make_library(tmp_path, list(workspace.LINKED_SKILLS))
    home = tmp_path / "codex_home"
    home.mkdir()
    workspace.ensure_skills(home, library)
    first = sorted(p.name for p in (home / "skills").iterdir())
    workspace.ensure_skills(home, library)
    second = sorted(p.name for p in (home / "skills").iterdir())
    assert first == second


def test_skills_guidance_points_at_real_library_path():
    from app.codex_service import context

    text = context.developer_instructions("general", codex_home="/data/users/u1/codex_home")
    assert "/data/users/u1/codex_home/skill-library" in text
    assert "降级规则" in text and "沙箱没有网络" in text


def test_curated_skills_exist_in_real_library():
    """精选清单里的技能必须在真实技能库里都存在（防止改名后静默挂空）。"""
    library = Path("/home/user/.agents/skills")
    if not library.is_dir():
        return
    missing = [name for name in workspace.LINKED_SKILLS if not (library / name).is_dir()]
    assert missing == [], f"技能库里找不到：{missing}"


def test_local_skill_refreshes_when_source_changes(tmp_path, monkeypatch):
    """自研技能改了内容要跟着更新，否则线上的模型一直读旧版提示词。"""
    source_root = tmp_path / "own-skills"
    skill = source_root / "explainer-video"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("v1", encoding="utf-8")
    monkeypatch.setattr(workspace, "LOCAL_SKILLS_DIR", source_root)

    home = tmp_path / "codex_home"
    home.mkdir()
    library = _make_library(tmp_path, ["hyperframes"])
    workspace.ensure_skills(home, library)
    installed = home / "skills" / "explainer-video" / "SKILL.md"
    assert installed.read_text(encoding="utf-8") == "v1"

    (skill / "SKILL.md").write_text("v2-更慢的讲解节奏", encoding="utf-8")
    workspace.ensure_skills(home, library)
    assert installed.read_text(encoding="utf-8") == "v2-更慢的讲解节奏"
