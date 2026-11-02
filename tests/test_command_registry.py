"""注册中心：注册、冲突、查找、补全。"""

import pytest

from helpcode.command.builtin import build_default_registry
from helpcode.command.registry import CommandRegistry, DuplicateCommandError
from helpcode.command.types import Command, CommandType


async def _noop(ctx):
    return None


def _cmd(name, **kw):
    base = {
        "name": name,
        "description": "d",
        "usage": f"/{name}",
        "type": CommandType.LOCAL,
        "handler": _noop,
    }
    base.update(kw)
    return Command(**base)


def test_register_and_resolve():
    r = CommandRegistry()
    r.register(_cmd("status"))
    assert r.resolve("status").name == "status"


def test_alias_resolves():
    r = CommandRegistry()
    r.register(_cmd("session", aliases=("resume",)))
    assert r.resolve("resume").name == "session"


def test_case_insensitive_resolve():
    r = CommandRegistry()
    r.register(_cmd("plan"))
    assert r.resolve("PLAN").name == "plan"
    assert r.resolve("Plan").name == "plan"


def test_duplicate_name_raises():
    r = CommandRegistry()
    r.register(_cmd("status"))
    with pytest.raises(DuplicateCommandError):
        r.register(_cmd("status"))


def test_alias_collides_with_name_raises():
    r = CommandRegistry()
    r.register(_cmd("status"))
    with pytest.raises(DuplicateCommandError):
        r.register(_cmd("other", aliases=("status",)))


def test_error_message_names_both():
    r = CommandRegistry()
    r.register(_cmd("status"))
    with pytest.raises(DuplicateCommandError) as ei:
        r.register(_cmd("other", aliases=("status",)))
    msg = str(ei.value)
    assert "status" in msg
    assert "other" in msg


def test_visible_excludes_hidden():
    r = CommandRegistry()
    r.register(_cmd("status"))
    r.register(_cmd("exit", hidden=True))
    assert [c.name for c in r.visible()] == ["status"]


def test_hidden_still_resolvable():
    r = CommandRegistry()
    r.register(_cmd("exit", hidden=True))
    assert r.resolve("exit") is not None


def test_visible_keeps_order():
    r = CommandRegistry()
    for n in ("help", "status", "compact"):
        r.register(_cmd(n))
    assert [c.name for c in r.visible()] == ["help", "status", "compact"]


def test_complete_prefix():
    r = CommandRegistry()
    r.register(_cmd("status"))
    r.register(_cmd("session", aliases=("resume",)))
    assert r.complete("/st") == ["/status"]
    assert r.complete("/res") == ["/resume"]


def test_complete_empty_prefix_lists_all():
    r = build_default_registry()
    keys = r.complete("/")
    # 可见命令 12 条（含 ch14 /worktree、ch15 /team）+ 可见别名
    assert len(keys) == 16
    assert "/exit" not in keys


def test_complete_excludes_hidden():
    r = CommandRegistry()
    r.register(_cmd("status"))
    r.register(_cmd("exit", hidden=True, aliases=("quit", "q")))
    for cand in r.complete("/"):
        assert cand not in ("/exit", "/quit", "/q")


def test_complete_includes_aliases():
    r = CommandRegistry()
    r.register(_cmd("session", aliases=("resume", "sessions")))
    assert "/resume" in r.complete("/res")
    assert "/sessions" in r.complete("/sess")


def test_complete_case_insensitive():
    r = CommandRegistry()
    r.register(_cmd("status"))
    assert r.complete("/ST") == r.complete("/st")


def test_complete_no_match_empty():
    r = CommandRegistry()
    r.register(_cmd("status"))
    assert r.complete("/zzz") == []


def test_complete_returns_slash_prefixed():
    r = CommandRegistry()
    r.register(_cmd("status"))
    assert all(c.startswith("/") for c in r.complete("/"))


def test_default_registry_counts():
    r = build_default_registry()
    # ch14 /worktree、ch15 /team：12 全量（含隐藏 exit）、12 可见
    assert len(r) == 13
    assert len(r.visible()) == 12
