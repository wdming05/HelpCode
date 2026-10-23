"""入口：加载配置、连接 MCP、启动 TUI。"""

import asyncio
import sys
from pathlib import Path

from .config import Config, ConfigError, load
from .mcp import McpManager, load_mcp_config
from .tui.app import HelpCodeApp

CONFIG_PATH = ".helpcode/config.yaml"


def main() -> None:
    """装配与启动 HelpCode。"""
    try:
        cfg = load(CONFIG_PATH)
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        # Ctrl+C 兜底（Textual 通常已处理）
        pass
    except Exception as e:
        print(f"启动失败: {e}", file=sys.stderr)
        sys.exit(1)


async def _run(cfg: Config) -> None:
    """同一个事件循环里：先连 MCP，再跑 TUI，最后统一关闭。

    MCP 会话绑定建立它的事件循环，所以不能在外层 ``asyncio.run`` 连完再
    让 ``app.run()`` 另起一个循环——那样 session 一进 TUI 就失效。
    """
    manager = McpManager(load_mcp_config(Path.cwd()))
    mcp_tools = await manager.connect_all()
    try:
        app = HelpCodeApp(cfg.providers, mcp_tools)
        await app.run_async()
    finally:
        await manager.aclose()
