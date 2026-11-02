"""`/team` 命令：注册、创建、列表、明细、终止、删除、未启用。"""

import asyncio

import pytest
from team_env import build

from helpcode.command import build_default_registry
from helpcode.command.builtin import CommandContext
from helpcode.config import ProviderConfig
from helpcode.tui.app import HelpCodeApp

_CFG = [ProviderConfig(name="qwen", protocol="openai", api_key="x", model="m")]


@pytest.fixture
def env(tmp_path):
    return build(tmp_path)


def _app(env):
    app = HelpCodeApp(_CFG, teams=env.mgr, worktrees=env.wt)
    app.team_manager = env.mgr
    app.team_service = env.svc
    return app


def test_command_registered():
    cmd = build_default_registry().resolve("team")
    assert cmd is not None
    assert cmd.type.value == "ui_state"


def test_disabled_message():
    app = HelpCodeApp(_CFG)
    text = asyncio.run(app.run_team("list", ""))
    assert "未启用" in text


def test_list_and_info(env):
    app = _app(env)
    listing = asyncio.run(app.run_team("list", ""))
    assert "refactor" in listing and "*当前" in listing

    info = asyncio.run(app.run_team("info", "refactor"))
    assert "lead" in info
    assert "后端" in info


def test_info_unknown(env):
    app = _app(env)
    text = asyncio.run(app.run_team("info", "nope"))
    assert "未找到团队" in text


def test_create_new_and_reuse(env):
    app = _app(env)
    created = asyncio.run(app.run_team("create", "alpha 第二个团队"))
    assert "已创建团队" in created and "alpha" in created

    again = asyncio.run(app.run_team("create", "alpha"))
    assert "alpha" in again  # 撞名直接切换，不报错
    assert env.svc.active_team.sanitized_name == "alpha"


def test_create_requires_name(env):
    app = _app(env)
    text = asyncio.run(app.run_team("create", "  "))
    assert "用法" in text


def test_kill(env):
    app = _app(env)

    async def scenario():
        await env.svc.spawn(
            env.team.sanitized_name,
            env.catalog.get("general-purpose"),
            "干活",
            member_name="alice",
        )
        await env.tasks.drain()
        return await app.run_team("kill", "alice")

    text = asyncio.run(scenario())
    assert "已终止" in text
    assert env.team.member("alice").is_active is False


def test_kill_unknown(env):
    app = _app(env)
    text = asyncio.run(app.run_team("kill", "nobody"))
    assert "未找到成员" in text


def test_delete(env):
    app = _app(env)
    text = asyncio.run(app.run_team("delete", "refactor"))
    assert "已删除团队" in text
    assert env.mgr.get("refactor") is None


def test_delete_requires_force(env):
    app = _app(env)

    async def scenario():
        await env.svc.spawn(
            env.team.sanitized_name,
            env.catalog.get("general-purpose"),
            "干活",
            member_name="bob",
        )
        blocked = await app.run_team("delete", "refactor")
        await env.tasks.drain()
        forced = await app.run_team("delete", "refactor --force")
        return blocked, forced

    blocked, forced = asyncio.run(scenario())
    assert "删除团队失败" in blocked and "活跃成员" in blocked
    assert "已删除团队" in forced


def test_unknown_subcommand(env):
    app = _app(env)
    text = asyncio.run(app.run_team("whatever", ""))
    assert "未知子命令" in text


def test_command_prints_usage_without_args():
    class _Host:
        def __init__(self):
            self.shown = []

        async def team(self, sub, args):
            raise AssertionError("无参数不该调宿主")

        def show(self, text):
            self.shown.append(text)

    reg = build_default_registry()
    cmd = reg.resolve("team")
    host = _Host()
    asyncio.run(cmd.handler(CommandContext(cmd, "", host, reg)))
    assert "用法" in host.shown[0]


def test_command_delegates_to_host(env):
    class _Host:
        def __init__(self):
            self.seen = []
            self.shown = []

        async def team(self, sub, args):
            self.seen.append((sub, args))
            return "ok"

        def show(self, text):
            self.shown.append(text)

    reg = build_default_registry()
    cmd = reg.resolve("team")
    host = _Host()
    asyncio.run(cmd.handler(CommandContext(cmd, "info refactor", host, reg)))
    assert host.seen == [("info", "refactor")]
    assert host.shown == ["ok"]
