"""/skills 命令、Skill 斜杠命令 handler 工厂、冲突。"""

import pytest

from helpcode.command import (
    CommandContext,
    DuplicateCommandError,
    build_default_registry,
)
from helpcode.skill import (
    SkillManager,
    load_skills,
    make_skill_command,
    register_skill_commands,
)


class _Host:
    def __init__(self):
        self.shown = []

    def show(self, text):
        self.shown.append(text)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return SkillManager(load_skills(tmp_path, tmp_path / "home"))


def _ctx(registry, host, cmd_name, args=""):

    cmd = registry.resolve(cmd_name)
    return CommandContext(cmd, args, host, registry)


def test_skills_lists(manager):
    reg = build_default_registry()
    register_skill_commands(reg, manager)
    host = _Host()
    import asyncio

    asyncio.run(reg.resolve("skills").handler(_ctx(reg, host, "skills")))
    text = host.shown[0]
    assert "commit" in text and "review" in text and "test" in text


def test_skills_reload(manager, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    manager._reload_fn = lambda: (
        calls.append(1),
        load_skills(tmp_path, tmp_path / "home"),
    )[1]
    reg = build_default_registry()
    register_skill_commands(reg, manager)
    host = _Host()
    import asyncio

    asyncio.run(reg.resolve("skills").handler(_ctx(reg, host, "skills", "reload")))
    assert calls == [1]
    assert any("重新加载" in t for t in host.shown)


def test_make_skill_command_names(manager):
    skill = manager.get("commit")
    cmd = make_skill_command(skill)
    assert cmd.name == "commit"
    assert cmd.type.name == "PROMPT"


def test_skill_command_collision_skipped(manager):
    # 内置 /review 已占用，再注册名为 review 的 Skill 命令会撞名
    reg = build_default_registry()
    skill = manager.get("review")
    with pytest.raises(DuplicateCommandError):
        reg.register(make_skill_command(skill))
