"""MCP 连接层测试。覆盖 AC8/AC10/F9/F11/N1/N7。"""

import asyncio

import pytest

from helpcode.mcp import manager as mgr_mod
from helpcode.mcp.config import ServerConfig
from helpcode.mcp.manager import McpConnection, McpManager
from helpcode.mcp.tool import RemoteTool


class FakeStack:
    """可配置'关闭时抛错/卡住'的假 stack。"""

    def __init__(self, *, fail: bool = False, hang: bool = False) -> None:
        self.fail = fail
        self.hang = hang
        self.closed = False

    async def aclose(self) -> None:
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise RuntimeError("close boom")
        self.closed = True


def _tool(server: str, name: str) -> RemoteTool:
    return RemoteTool(
        server=server,
        remote_name=name,
        name=f"mcp__{server}__{name}",
        description="d",
        parameters={},
        read_only=True,
        session=object(),
    )


def _cfg(name: str = "s") -> ServerConfig:
    return ServerConfig(name=name, type="stdio", command="python")


def test_empty_configs_returns_empty():
    assert asyncio.run(McpManager({}).connect_all()) == []


def test_failure_isolation(monkeypatch, capsys):
    """一个 server 抛异常，另一个的工具仍在结果里。"""

    async def fake_handshake(self, cfg):
        if cfg.name == "bad":
            raise RuntimeError("cannot start")
        return McpConnection(cfg.name, object(), FakeStack()), [_tool(cfg.name, "t")]

    monkeypatch.setattr(McpManager, "_handshake", fake_handshake)
    m = McpManager({"bad": _cfg("bad"), "good": _cfg("good")})
    tools = asyncio.run(m.connect_all())

    assert [t.name for t in tools] == ["mcp__good__t"]
    assert [c.name for c in m.connections] == ["good"]
    assert "bad" in capsys.readouterr().err


def test_connect_timeout(monkeypatch):
    """握手卡住时按常量收尾，不无限等待。"""

    async def hang(self, cfg):
        await asyncio.sleep(3600)

    monkeypatch.setattr(McpManager, "_handshake", hang)
    monkeypatch.setattr(mgr_mod, "MCP_CONNECT_TIMEOUT", 0.02)

    m = McpManager({"s": _cfg()})
    tools = asyncio.run(asyncio.wait_for(m.connect_all(), timeout=1.0))
    assert tools == []
    assert m.connections == []


def test_tools_sorted_by_server_and_name(monkeypatch):
    async def fake_handshake(self, cfg):
        return (
            McpConnection(cfg.name, object(), FakeStack()),
            [_tool(cfg.name, "z"), _tool(cfg.name, "a")],
        )

    monkeypatch.setattr(McpManager, "_handshake", fake_handshake)
    m = McpManager({"beta": _cfg("beta"), "alpha": _cfg("alpha")})
    tools = asyncio.run(m.connect_all())
    assert [t.name for t in tools] == [
        "mcp__alpha__a",
        "mcp__alpha__z",
        "mcp__beta__a",
        "mcp__beta__z",
    ]


def test_connections_returns_copy(monkeypatch):
    async def fake_handshake(self, cfg):
        return McpConnection(cfg.name, object(), FakeStack()), []

    monkeypatch.setattr(McpManager, "_handshake", fake_handshake)
    m = McpManager({"s": _cfg()})
    asyncio.run(m.connect_all())

    snapshot = m.connections
    snapshot.clear()
    assert len(m.connections) == 1


def test_aclose_closes_every_connection(monkeypatch):
    async def fake_handshake(self, cfg):
        return McpConnection(cfg.name, object(), FakeStack()), []

    monkeypatch.setattr(McpManager, "_handshake", fake_handshake)
    m = McpManager({"a": _cfg("a"), "b": _cfg("b")})
    asyncio.run(m.connect_all())
    conns = m.connections
    asyncio.run(m.aclose())

    assert all(c.stack.closed for c in conns)
    assert m.connections == []


def test_aclose_isolates_errors(monkeypatch):
    """一个连接关闭抛错，另一个仍要被关闭。"""

    async def fake_handshake(self, cfg):
        stack = FakeStack(fail=cfg.name == "bad")
        return McpConnection(cfg.name, object(), stack), []

    monkeypatch.setattr(McpManager, "_handshake", fake_handshake)
    m = McpManager({"bad": _cfg("bad"), "good": _cfg("good")})
    asyncio.run(m.connect_all())
    conns = {c.name: c for c in m.connections}

    asyncio.run(m.aclose())
    assert conns["good"].stack.closed


def test_aclose_timeout_does_not_hang(monkeypatch):
    async def fake_handshake(self, cfg):
        return McpConnection(cfg.name, object(), FakeStack(hang=True)), []

    monkeypatch.setattr(McpManager, "_handshake", fake_handshake)
    monkeypatch.setattr(mgr_mod, "MCP_CLOSE_TIMEOUT", 0.02)

    m = McpManager({"s": _cfg()})
    asyncio.run(m.connect_all())
    asyncio.run(asyncio.wait_for(m.aclose(), timeout=1.0))
    assert m.connections == []


def test_stdio_params_merge_env(monkeypatch):
    """env 与宿主环境合并，同名被配置覆盖（不真的起子进程，只截获参数）。"""
    from contextlib import asynccontextmanager

    captured = {}

    @asynccontextmanager
    async def fake_stdio_client(params):
        captured["params"] = params
        raise RuntimeError("stop here")
        yield  # pragma: no cover

    import mcp.client.stdio as stdio_mod

    monkeypatch.setattr(stdio_mod, "stdio_client", fake_stdio_client)
    monkeypatch.setenv("SHARED", "from-host")

    m = McpManager({})
    cfg = ServerConfig(
        name="s", type="stdio", command="python", env={"SHARED": "from-config"}
    )
    with pytest.raises(RuntimeError):
        asyncio.run(m._handshake(cfg))

    assert captured["params"].env["SHARED"] == "from-config"
    assert captured["params"].command == "python"


def test_connect_all_runs_concurrently(monkeypatch):
    """多个 server 并发连接：总耗时接近单个而非累加。"""
    import time

    async def slow_handshake(self, cfg):
        await asyncio.sleep(0.2)
        return McpConnection(cfg.name, object(), FakeStack()), []

    monkeypatch.setattr(McpManager, "_handshake", slow_handshake)
    m = McpManager({n: _cfg(n) for n in ("a", "b", "c")})

    start = time.perf_counter()
    asyncio.run(m.connect_all())
    elapsed = time.perf_counter() - start

    assert len(m.connections) == 3
    assert elapsed < 0.45  # 串行至少 0.6s


def test_http_headers_passed_to_transport(monkeypatch):
    """http 类型的 headers 必须注入到传输层（AC5 的另一半：不需真服务器）。"""
    from contextlib import asynccontextmanager

    captured = {}

    @asynccontextmanager
    async def fake_http_client(url, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        raise RuntimeError("stop here")
        yield  # pragma: no cover

    import mcp.client.streamable_http as http_mod

    monkeypatch.setattr(http_mod, "streamablehttp_client", fake_http_client)

    cfg = ServerConfig(
        name="gh",
        type="http",
        url="https://example.com/mcp",
        headers={"Authorization": "Bearer x"},
    )
    with pytest.raises(RuntimeError):
        asyncio.run(McpManager({})._handshake(cfg))

    assert captured["url"] == "https://example.com/mcp"
    assert captured["headers"] == {"Authorization": "Bearer x"}
