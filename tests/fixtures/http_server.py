"""最小 Streamable HTTP MCP server，供手工/集成验证使用（AC5）。"""

import os

from mcp.server.fastmcp import FastMCP

PORT = int(os.environ.get("MCP_PORT", "8931"))

mcp = FastMCP("http-echo", host="127.0.0.1", port=PORT)


@mcp.tool()
def echo(text: str) -> str:
    """原样返回传入的文本。"""
    return text


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
