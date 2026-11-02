"""`/worktree` 命令：注册、列表、创建/退出/删除、未启用提示。"""

import asyncio
from pathlib import Path

import pytest
from git_repo import make_repo

from helpcode.command import build_default_registry
from helpcode.command.builtin import CommandContext
from helpcode.config import ProviderConfig
from helpcode.tui.app import HelpCodeApp
from helpcode.worktree import Manager

_CFG = [ProviderConfig(name="qwen", protocol="openai", api_key="x", model="m")]


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


def _app(worktrees=None):
    return HelpCodeApp(_CFG, worktrees=worktrees)


def test_command_registered():
    reg = build_default_registry()
    cmd = reg.resolve("worktree")
    assert cmd is not None
    assert cmd.type.value == "ui_state"
    assert "create" in cmd.usage


def test_disabled_message():
    app = _app(None)
    text = asyncio.run(app.run_worktree("list", ""))
    assert "未启用" in text


def test_usage_on_unknown(repo):
    app = _app(Manager(repo))
    text = asyncio.run(app.run_worktree("whatever", ""))
    assert "未知子命令" in text


def test_create_then_list(repo):
    app = _app(Manager(repo))
    created = asyncio.run(app.run_worktree("create", "fix-login"))

    assert "已创建并进入" in created
    assert "worktree-fix-login" in created
    assert app.worktree_cwd and app.worktree_cwd.endswith("fix-login")

    listing = asyncio.run(app.run_worktree("list", ""))
    assert "fix-login" in listing
    assert "*当前" in listing
    assert "手动" in listing


def test_create_requires_name(repo):
    app = _app(Manager(repo))
    text = asyncio.run(app.run_worktree("create", "   "))
    assert "用法" in text


def test_create_rejects_bad_slug(repo):
    app = _app(Manager(repo))
    text = asyncio.run(app.run_worktree("create", "../evil"))
    assert "创建失败" in text
    assert app.worktree_cwd is None


def test_exit_keep(repo):
    app = _app(Manager(repo))
    asyncio.run(app.run_worktree("create", "keep-me"))
    text = asyncio.run(app.run_worktree("exit", ""))

    assert "已退出" in text and "保留" in text
    assert app.worktree_cwd is None
    assert (repo / ".helpcode" / "worktrees" / "keep-me").is_dir()


def test_exit_remove_blocked_then_discard(repo):
    app = _app(Manager(repo))
    asyncio.run(app.run_worktree("create", "dirty"))
    wt = Path(app.worktree_cwd)
    (wt / "x.txt").write_text("x", encoding="utf-8")

    blocked = asyncio.run(app.run_worktree("exit", "--remove"))
    assert "退出失败" in blocked and "--discard" in blocked
    assert wt.is_dir()

    removed = asyncio.run(app.run_worktree("remove", "dirty --discard"))
    assert "已删除" in removed
    assert not wt.exists()
    assert app.worktree_cwd is None


def test_remove_requires_name(repo):
    app = _app(Manager(repo))
    text = asyncio.run(app.run_worktree("remove", "--discard"))
    assert "用法" in text


def test_exit_without_session(repo):
    app = _app(Manager(repo))
    text = asyncio.run(app.run_worktree("exit", ""))
    assert "不在任何 Worktree" in text


def test_enter_existing(repo):
    """create 之后退出，再用 enter 回到同一个 Worktree。"""
    app = _app(Manager(repo))
    asyncio.run(app.run_worktree("create", "demo"))
    asyncio.run(app.run_worktree("exit", ""))
    assert app.worktree_cwd is None

    text = asyncio.run(app.run_worktree("enter", "demo"))
    assert "已进入" in text
    assert app.worktree_cwd and app.worktree_cwd.endswith("demo")
    assert app.worktrees.current_session.worktree_name == "demo"


def test_enter_unknown(repo):
    app = _app(Manager(repo))
    text = asyncio.run(app.run_worktree("enter", "nope"))
    assert "没有这个 Worktree" in text


def test_create_existing_hints_enter(repo):
    """撞名不该只说「创建失败」——用户真正想做的是进去。"""
    app = _app(Manager(repo))
    asyncio.run(app.run_worktree("create", "demo"))
    text = asyncio.run(app.run_worktree("create", "demo"))

    assert "已存在" in text
    assert "/worktree enter demo" in text
    assert app.worktrees.current_session is not None  # 当前会话没被踢掉


def test_list_hints_when_no_session(repo):
    app = _app(Manager(repo))
    asyncio.run(app.run_worktree("create", "demo"))
    asyncio.run(app.run_worktree("exit", ""))
    listing = asyncio.run(app.run_worktree("list", ""))
    assert "enter" in listing


def test_enter_and_exit_announce(repo):
    """进出都要跟模型说一声，否则它按主仓库路径汇报位置。"""
    app = _app(Manager(repo))
    asyncio.run(app.run_worktree("create", "demo"))
    assert any("工作目录" in n for n in app._pending_notices)

    app._pending_notices.clear()
    asyncio.run(app.run_worktree("exit", ""))
    assert any("回到主工作目录" in n for n in app._pending_notices)


def test_command_handler_uses_host(repo):
    """命令层只做解析：把 sub/args 原样交给宿主，再把返回文本展示出来。"""

    class _Host:
        def __init__(self):
            self.seen = []
            self.shown = []

        async def worktree(self, sub, args):
            self.seen.append((sub, args))
            return "ok"

        def show(self, text):
            self.shown.append(text)

    reg = build_default_registry()
    cmd = reg.resolve("worktree")
    host = _Host()
    asyncio.run(cmd.handler(CommandContext(cmd, "create feat/a", host, reg)))
    assert host.seen == [("create", "feat/a")]
    assert host.shown == ["ok"]


def test_command_prints_usage_without_args():
    class _Host:
        def __init__(self):
            self.shown = []

        async def worktree(self, sub, args):
            raise AssertionError("无参数不该调宿主")

        def show(self, text):
            self.shown.append(text)

    reg = build_default_registry()
    cmd = reg.resolve("worktree")
    host = _Host()
    asyncio.run(cmd.handler(CommandContext(cmd, "", host, reg)))
    assert "用法" in host.shown[0]
