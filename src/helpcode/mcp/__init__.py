"""MCP 客户端：配置加载 + 连接管理 + 远端工具适配。

依赖方向约束：本包只允许 import ``helpcode.tool``、``mcp`` SDK 与标准库，
**不得** import agent / tui / permission / conversation / llm / prompt。
"""

from .config import ServerConfig, load_mcp_config
from .manager import McpConnection, McpManager
from .tool import RemoteTool, build_remote_tools

__all__ = [
    "McpConnection",
    "McpManager",
    "RemoteTool",
    "ServerConfig",
    "build_remote_tools",
    "load_mcp_config",
]
