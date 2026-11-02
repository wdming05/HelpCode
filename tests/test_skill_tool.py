"""load_skill 工具：命中 / 未命中 / 激活回调 / 只读 / 系统级。"""

from helpcode.skill import SkillManager, SkillTool, load_skills
from helpcode.skill.types import LOAD_SKILL_TOOL


def _tool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = SkillManager(load_skills(tmp_path, tmp_path / "home"))
    return manager, SkillTool(manager)


def test_load_returns_body(tmp_path, monkeypatch):
    manager, tool = _tool(tmp_path, monkeypatch)
    result = tool.run("commit")
    assert result.ok
    assert "提交信息生成助手" in result.content
    assert manager.is_active("commit")


def test_load_unknown(tmp_path, monkeypatch):
    manager, tool = _tool(tmp_path, monkeypatch)
    result = tool.run("nope")
    assert not result.ok
    assert result.is_error
    assert not manager.is_active("nope")


def test_load_activates_and_callbacks(tmp_path, monkeypatch):
    manager, tool = _tool(tmp_path, monkeypatch)
    seen = []

    tool._on_activate = lambda skill: seen.append(skill.name)
    tool.run("review")
    assert seen == ["review"]
    assert manager.is_active("review")


def test_skill_tool_read_only():
    assert SkillTool.name == LOAD_SKILL_TOOL
    assert SkillTool.read_only is True


def test_load_skill_survives_whitelist(tmp_path, monkeypatch):
    manager, _ = _tool(tmp_path, monkeypatch)
    manager.activate("commit")
    assert LOAD_SKILL_TOOL in manager.allowed_tools()
