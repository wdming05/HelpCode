"""MCP 连接管理：并发连接、单 server 超时、失败隔离、统一关闭。

只依赖标准库、``mcp`` SDK 与本包的 config/tool。
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from .config import ServerConfig
from .tool import RemoteTool, build_remote_tools

# 单 server 整个启动序列（连接 + 握手 + 列工具）的上界，spec F9
MCP_CONNECT_TIMEOUT = 30.0
# 整体关闭的兜底上界，spec F11
MCP_CLOSE_TIMEOUT = 5.0


def _warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


@dataclass
class McpConnection:
    """一个已建立的 server 连接；stack 负责持有传输与会话的生命周期。"""

    name: str
    session: Any
    stack: AsyncExitStack


class McpManager:
    """按配置连接全部 server；失败只影响自己，退出时统一关闭。"""

    def __init__(self, configs: dict[str, ServerConfig]) -> None:
        self._configs = dict(configs)
        self._conns: list[McpConnection] = []
        self._tools: list[RemoteTool] = []

    @property
    def connections(self) -> list[McpConnection]:
        """返回副本：外部增删不应影响管理器内部状态。"""
        return list(self._conns)

    async def connect_all(self) -> list[RemoteTool]:
        """并发连接全部 server，返回成功连上的工具（按 server/name 排序）。"""
        if not self._configs:
            return []

        names = sorted(self._configs)
        results = await asyncio.gather(
            *[self._connect_one(self._configs[n]) for n in names],
            return_exceptions=True,
        )

        tools: list[RemoteTool] = []
        for name, res in zip(names, results, strict=True):
            if isinstance(res, BaseException):
                _warn(f"MCP server {name!r} 启动失败，已跳过：{res}")
                continue
            conn, server_tools = res
            self._conns.append(conn)
            tools.extend(server_tools)

        tools.sort(key=lambda t: (t.server, t.name))
        self._tools = tools
        return list(tools)

    async def _connect_one(
        self, cfg: ServerConfig
    ) -> tuple[McpConnection, list[RemoteTool]]:
        """单个 server 的启动序列，受 MCP_CONNECT_TIMEOUT 约束。"""
        try:
            return await asyncio.wait_for(
                self._handshake(cfg), timeout=MCP_CONNECT_TIMEOUT
            )
        except TimeoutError:
            raise TimeoutError(
                f"启动超时（>{MCP_CONNECT_TIMEOUT}s）：连接 / 握手 / 列工具未完成"
            ) from None

    async def _handshake(
        self, cfg: ServerConfig
    ) -> tuple[McpConnection, list[RemoteTool]]:
        """建立传输 → 建会话 → 初始化 → 列工具。

        stack 必须随连接存续到进程退出：若在这里 ``async with`` 收尾，
        块一退出传输就关了，返回的 session 立刻失效。
        """
        stack = AsyncExitStack()
        try:
            read, write = await self._open_transport(stack, cfg)

            from mcp import ClientSession

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
            tools = build_remote_tools(cfg.name, session, list(listed.tools))
            return McpConnection(name=cfg.name, session=session, stack=stack), tools
        except BaseException:
            # 失败必须把已开的资源关掉，否则子进程会泄漏
            await stack.aclose()
            raise

    async def _open_transport(self, stack: AsyncExitStack, cfg: ServerConfig):
        """打开 stdio 或 HTTP 传输，返回读写流。"""
        if cfg.type == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=cfg.command,
                args=list(cfg.args),
                env={**os.environ, **cfg.env},
            )
            streams = await stack.enter_async_context(stdio_client(params))
        else:
            from mcp.client.streamable_http import streamablehttp_client

            streams = await stack.enter_async_context(
                streamablehttp_client(cfg.url, headers=cfg.headers or None)
            )
        # stdio 返回 2 元组，streamable http 返回 (read, write, get_session_id)
        return streams[0], streams[1]

    async def aclose(self) -> None:
        """统一关闭全部连接；整体受 MCP_CLOSE_TIMEOUT 约束。"""
        conns, self._conns = self._conns, []
        self._tools = []
        if not conns:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *[c.stack.aclose() for c in conns], return_exceptions=True
                ),
                timeout=MCP_CLOSE_TIMEOUT,
            )
        except TimeoutError:
            _warn(f"MCP 连接关闭超时（>{MCP_CLOSE_TIMEOUT}s），放弃等待其余连接")
