"""三级加载、覆盖、目录型 Skill、坏文件跳过、白名单校验。"""

from pathlib import Path

from helpcode.skill import load_skills, validate_whitelists
from helpcode.skill.types import WhitelistError


def _write(path: Path, name: str, desc: str = "d", body: str = "正文") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n", encoding="utf-8"
    )


def test_builtin_skills():
    reg = load_skills(Path("."), Path("."))
    names = {s.name for s in reg.all()}
    assert {"commit", "review", "test"} <= names


def test_three_level_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    user_dir = home / ".helpcode" / "skills"
    proj_dir = tmp_path / ".helpcode" / "skills"
    _write(user_dir / "x.md", "x", "用户版", "用户正文")
    _write(proj_dir / "x.md", "x", "项目版", "项目正文")

    reg = load_skills(tmp_path, home)
    skill = reg.get("x")
    assert skill is not None
    assert skill.source == "project"
    assert skill.body == "项目正文"


def test_broken_file_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    proj_dir = tmp_path / ".helpcode" / "skills"
    proj_dir.mkdir(parents=True)
    (proj_dir / "bad.md").write_text("没有 frontmatter\n", encoding="utf-8")
    _write(proj_dir / "good.md", "good")

    reg = load_skills(tmp_path, tmp_path / "home")
    assert reg.get("good") is not None
    assert reg.get("bad") is None
    assert any("bad" in w for w in reg.warnings)


def test_duplicate_in_level_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    proj_dir = tmp_path / ".helpcode" / "skills"
    _write(proj_dir / "a.md", "dup", "第一份", "一")
    _write(proj_dir / "b.md", "dup", "第二份", "二")

    reg = load_skills(tmp_path, tmp_path / "home")
    assert reg.get("dup").body == "一"  # 保留先到者
    assert any("重复" in w for w in reg.warnings)


def test_directory_skill(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    skill_dir = tmp_path / ".helpcode" / "skills" / "mydir"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: mydir\ndescription: 目录型\n---\nSOP 正文\n", encoding="utf-8"
    )
    (skill_dir / "template.txt").write_text("模板", encoding="utf-8")

    reg = load_skills(tmp_path, tmp_path / "home")
    skill = reg.get("mydir")
    assert skill is not None
    assert skill.directory is not None
    assert skill.directory.name == "mydir"


def test_registry_get_case_insensitive():
    reg = load_skills(Path("."), Path("."))
    assert reg.get("COMMIT") is not None
    assert reg.get("Commit") is not None


def test_validate_ok():
    from helpcode.skill import SkillRegistry
    from helpcode.skill.types import Skill

    reg = SkillRegistry()
    reg.set(Skill(name="x", description="d", body="b", tools=("grep", "read_file")))
    validate_whitelists(reg, {"grep", "read_file", "bash"})  # 不抛


def test_validate_unknown_tool():
    from helpcode.skill import SkillRegistry
    from helpcode.skill.types import Skill

    reg = SkillRegistry()
    reg.set(Skill(name="x", description="d", body="b", tools=("nope",)))
    try:
        validate_whitelists(reg, {"grep", "read_file"})
        raise AssertionError("应当抛出 WhitelistError")
    except WhitelistError as e:
        assert e.tool == "nope"
        assert e.skill_name == "x"


def test_import_no_side_effect(tmp_path, monkeypatch):
    """import helpcode.skill 及 load_skills 都不创建任何目录/文件（AC18/N1）。"""
    monkeypatch.chdir(tmp_path)
    reg = load_skills(tmp_path, tmp_path / "home")
    assert not (tmp_path / ".helpcode").exists()
    assert not (tmp_path / "home" / ".helpcode").exists()
    assert len(reg.all()) >= 3  # 只注入内置，不落盘
