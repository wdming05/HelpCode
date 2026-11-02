"""MCP 端到端测试：真起一个 stdio server，走完连接 → 列工具 → 调用 → 关闭。

不依赖网络、不依赖任何外部 token，可在离线机器上跑。覆盖 AC4/AC6/AC10。
"""

import asyncio
import sys
from pathlib import Path

import pytest

from helpcode.mcp import McpManager, load_mcp_config
from helpcode.mcp.config import CONFIG_RELPATH

FIXTURE = Path(__file__).parent / "fixtures" / "echo_server.py"


def _prepare(tmp_path: Path, monkeypatch) -> Path:
    """造一个只有 stdio echo server 的项目配置，并把 HOME 指向空目录。"""
    home = tmp_path / "home"
    root = tmp_path / "proj"
    home.mkdir(parents=True, exist_ok=True)
    (root / CONFIG_RELPATH).parent.mkdir(parents=True, exist_ok=True)

    config = f"""
mcp_servers:
  echo:
    type: stdio
    command: {sys.executable}
    args:
      - {FIXTURE}
    env:
      ECHO_TOKEN: injected-value
"""
    (root / CONFIG_RELPATH).write_text(config, encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    return root


@pytest.mark.timeout(60)
def test_stdio_end_to_end(tmp_path, monkeypatch):
    root = _prepare(tmp_path, monkeypatch)
    manager = McpManager(load_mcp_config(root))

    async def scenario() -> None:
        tools = await manager.connect_all()
        names = {t.name for t in tools}

        # 1) 工具被正确命名与注册
        assert "mcp__echo__echo" in names
        assert "mcp__echo__add" in names
        assert "mcp__echo__get_env" in names
        assert len(manager.connections) == 1

        # 2) 描述非空、schema 透传
        echo_tool = next(t for t in tools if t.name == "mcp__echo__echo")
        assert echo_tool.description
        assert echo_tool.parameters.get("type") == "object"

        # 3) 真正调一次，文本回灌
        result = await echo_tool.arun(text="你好")
        assert result.ok and not result.is_error
        assert "你好" in result.content

        # 4) env 注入生效
        env_tool = next(t for t in tools if t.name == "mcp__echo__get_env")
        env_result = await env_tool.arun(name="ECHO_TOKEN")
        assert env_result.content == "injected-value"

        # 5) 关闭后连接清空，且再调用只会得到结构化错误（不崩）
        await manager.aclose()
        assert manager.connections == []
        after = await echo_tool.arun(text="x")
        assert after.is_error

    asyncio.run(scenario())


@pytest.mark.timeout(60)
def test_bad_server_does_not_block_good_one(tmp_path, monkeypatch):
    """一个 server 起不来，另一个仍可用。"""
    root = _prepare(tmp_path, monkeypatch)
    cfg_path = root / CONFIG_RELPATH
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8")
        + """
  broken:
    type: stdio
    command: definitely-not-a-real-executable-xyz
""",
        encoding="utf-8",
    )

    manager = McpManager(load_mcp_config(root))

    async def scenario() -> None:
        tools = await manager.connect_all()
        names = {t.name for t in tools}
        assert "mcp__echo__echo" in names
        assert not any(n.startswith("mcp__broken__") for n in names)
        await manager.aclose()

    asyncio.run(scenario())
