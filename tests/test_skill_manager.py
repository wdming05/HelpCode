"""SkillManager：渲染、激活、白名单、reload。"""

from pathlib import Path

from helpcode.skill import SkillManager, load_skills
from helpcode.skill.types import LOAD_SKILL_TOOL


def _manager(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return SkillManager(load_skills(tmp_path, tmp_path / "home"))


def test_render_available_no_body():
    reg = load_skills(Path("."), Path("."))
    m = SkillManager(reg)
    text = m.render_available()
    assert "commit" in text and "按规范生成" in text
    assert "SOP" not in text and "提交信息生成助手" not in text  # 不含正文


def test_render_active_has_body():
    reg = load_skills(Path("."), Path("."))
    m = SkillManager(reg)
    m.activate("commit")
    text = m.render_active()
    assert "# Skill: commit" in text
    assert "提交信息生成助手" in text  # 正文进来了


def test_multiple_active():
    reg = load_skills(Path("."), Path("."))
    m = SkillManager(reg)
    m.activate("commit")
    m.activate("test")
    text = m.render_active()
    assert "# Skill: commit" in text
    assert "# Skill: test" in text


def test_render_active_empty_when_none():
    reg = load_skills(Path("."), Path("."))
    assert SkillManager(reg).render_active() == ""


def test_allowed_tools_union():
    reg = load_skills(Path("."), Path("."))
    m = SkillManager(reg)
    m.activate("commit")  # read_file, grep, bash
    m.activate("review")  # read_file, grep, glob
    allowed = m.allowed_tools()
    assert allowed == {"read_file", "grep", "bash", "glob", LOAD_SKILL_TOOL}


def test_allowed_tools_none_when_empty():
    reg = load_skills(Path("."), Path("."))
    assert SkillManager(reg).allowed_tools() is None


def test_load_skill_survives_whitelist():
    reg = load_skills(Path("."), Path("."))
    m = SkillManager(reg)
    m.activate("test")  # bash, read_file, grep
    assert LOAD_SKILL_TOOL in m.allowed_tools()


def test_clear():
    reg = load_skills(Path("."), Path("."))
    m = SkillManager(reg)
    m.activate("commit")
    m.clear()
    assert m.render_active() == ""
    assert m.allowed_tools() is None
    assert m.active_names() == ()


def test_load_text_directory_hint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / ".helpcode" / "skills" / "d"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: d\ndescription: d\n---\n范围 {{args}}\n", encoding="utf-8"
    )
    m = SkillManager(load_skills(tmp_path, tmp_path / "home"))
    skill = m.get("d")
    text = m.load_text(skill, "src")
    assert "范围 src" in text
    assert str(d) in text  # 目录路径带出


def test_activate_unknown_returns_false():
    reg = load_skills(Path("."), Path("."))
    assert SkillManager(reg).activate("nope") is False


def test_reload_calls_fn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    def _reload():
        calls.append(1)
        return load_skills(tmp_path, tmp_path / "home")

    m = SkillManager(load_skills(tmp_path, tmp_path / "home"), reload_fn=_reload)
    m.reload()
    assert calls == [1]
