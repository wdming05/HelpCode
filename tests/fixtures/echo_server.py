"""最小 stdio MCP server，供 E2E 测试使用（不依赖网络与外部 token）。"""

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """原样返回传入的文本。"""
    return text


@mcp.tool()
def add(a: int, b: int) -> int:
    """返回两个整数之和。"""
    return a + b


@mcp.tool()
def get_env(name: str) -> str:
    """读取本进程的环境变量（用于验证 env 注入）。"""
    return os.environ.get(name, "")


if __name__ == "__main__":
    mcp.run()
