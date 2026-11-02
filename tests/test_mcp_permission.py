"""MCP 工具接入权限链路的集成测试（覆盖 AC11/F12/N4）。

这里不重造权限逻辑，只验证「远端工具注册后，既有链路对它的裁决是否正确」：
- 只读标志 → ``Registry.is_read_only`` → 并发/串行分流
- 工具名 → ``check`` → 规则 / 模式兜底
- 黑名单只认内置 bash
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from helpcode.mcp.tool import RemoteTool
from helpcode.permission import Decision, Mode, check, new_engine
from helpcode.tool import Registry, new_default_registry


def _call(name: str, **args) -> SimpleNamespace:
    return SimpleNamespace(name=name, input=args)


def _remote(server="echo", name="echo", read_only=False) -> RemoteTool:
    return RemoteTool(
        server=server,
        remote_name=name,
        name=f"mcp__{server}__{name}",
        description="d",
        parameters={"type": "object"},
        read_only=read_only,
        session=object(),
    )


def _engine(tmp_path: Path, monkeypatch, allow=(), deny=()):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    settings = tmp_path / ".helpcode"
    settings.mkdir(parents=True, exist_ok=True)
    body = "permissions:\n"
    body += "  allow:\n" + "".join(f'    - "{r}"\n' for r in allow)
    body += "  deny:\n" + "".join(f'    - "{r}"\n' for r in deny)
    (settings / "settings.yaml").write_text(body, encoding="utf-8")
    engine, err = new_engine(str(tmp_path))
    assert err is None
    return engine


def test_exact_name_rule_allows(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch, allow=["mcp__echo__echo"])
    decision, _ = check(engine, Mode.DEFAULT, _call("mcp__echo__echo"), False)
    assert decision is Decision.ALLOW


def test_glob_rule_allows_and_denies(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch, allow=["mcp__echo__*"])
    assert check(engine, Mode.DEFAULT, _call("mcp__echo__echo"), False)[0] is (
        Decision.ALLOW
    )

    engine = _engine(tmp_path / "deny", monkeypatch, deny=["mcp__echo__*"])
    decision, reason = check(engine, Mode.DEFAULT, _call("mcp__echo__echo"), False)
    assert decision is Decision.DENY
    assert "deny" in reason


def test_glob_rule_is_case_sensitive(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch, allow=["mcp__echo__*"])
    decision, _ = check(engine, Mode.DEFAULT, _call("mcp__Echo__echo"), False)
    assert decision is not Decision.ALLOW


def test_read_only_flag_maps_to_read_category(tmp_path, monkeypatch):
    """只读 → READ 类 → default 直接放行（不会 Ask）。"""
    engine = _engine(tmp_path, monkeypatch)
    assert check(engine, Mode.DEFAULT, _call("mcp__echo__echo"), True)[0] is (
        Decision.ALLOW
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (Mode.DEFAULT, Decision.ASK),
        (Mode.ACCEPT_EDITS, Decision.ASK),
        (Mode.BYPASS, Decision.ALLOW),
        (Mode.PLAN, Decision.ASK),
    ],
)
def test_non_readonly_mode_matrix(tmp_path, monkeypatch, mode, expected):
    """非只读 MCP 工具归 EXEC 类：default/acceptEdits/plan 都 Ask，bypass 放行。"""
    engine = _engine(tmp_path, monkeypatch)
    assert check(engine, mode, _call("mcp__echo__write"), False)[0] is expected


def test_blacklist_does_not_hit_mcp_tool(tmp_path, monkeypatch):
    """MCP 工具即使有 command 参数、取值命中黑名单，也不被内置黑名单拦。"""
    engine = _engine(tmp_path, monkeypatch)
    decision, _ = check(
        engine, Mode.BYPASS, _call("mcp__shell__run", command="rm -rf /"), False
    )
    assert decision is Decision.ALLOW


def test_blacklist_still_hits_builtin_bash(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    decision, reason = check(
        engine, Mode.BYPASS, _call("bash", command="rm -rf /"), False
    )
    assert decision is Decision.DENY
    assert "黑名单" in reason


def test_registry_order_and_read_only_lookup():
    """内置 6 个在前、MCP 在后；is_read_only 对远端工具同样生效。"""
    registry: Registry = new_default_registry()
    builtin = registry.names()
    assert len(builtin) == 6

    registry.register(_remote("echo", "peek", read_only=True))
    registry.register(_remote("echo", "write", read_only=False))

    assert registry.names()[:6] == builtin
    assert registry.is_read_only("mcp__echo__peek") is True
    assert registry.is_read_only("mcp__echo__write") is False
    assert registry.get("mcp__echo__peek") is not None
