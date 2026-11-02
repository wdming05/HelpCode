"""远端工具适配：把 MCP 工具包装成 HelpCode 的（异步）工具。

只依赖标准库与 ``helpcode.tool``——不认识 agent / tui / permission。
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

from ..tool import ToolResult

# 单次 call_tool 上界（内置不可配，spec F10）
MCP_CALL_TIMEOUT = 30.0

# LLM 的工具名只接受这些字符；不合法的工具在这一层就拦掉，
# 否则错误会一路带到请求时才 400，且看不出是哪个 server 的问题。
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


class RemoteTool:
    """一个远端 MCP 工具的本地代理。

    只实现 ``arun``（满足 ``tool.AsyncTool``）；``run`` 故意抛错，防止被当成同步工具
    丢进线程池——MCP 会话绑定事件循环，在工作线程里调用必然失败。
    """

    def __init__(
        self,
        *,
        server: str,
        remote_name: str,
        name: str,
        description: str,
        parameters: dict,
        read_only: bool,
        session: Any,
        timeout: float = MCP_CALL_TIMEOUT,
    ) -> None:
        self.server = server
        self.remote_name = remote_name
        self.name = name
        self.description = description
        self.parameters = parameters
        self.read_only = read_only
        self._session = session
        self._timeout = timeout
        self._warned_non_text = False

    def run(self, **kwargs: Any) -> ToolResult:
        raise RuntimeError(f"MCP 工具 {self.name} 必须异步调用（arun）")

    async def arun(self, **kwargs: Any) -> ToolResult:
        """调用远端工具；任何失败都转成结构化错误，不向 Agent Loop 抛异常。"""
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self.remote_name, dict(kwargs or {})),
                timeout=self._timeout,
            )
        except TimeoutError:
            return ToolResult(
                ok=False,
                content=f"错误：MCP 工具 {self.name} 调用超时（>{self._timeout}s）",
                is_error=True,
            )
        except Exception as e:
            # F7 要求任何失败都不中断 Loop；CancelledError 继承自 BaseException，
            # 不会被这里吞掉（用户取消仍能向上传播）。
            return ToolResult(
                ok=False,
                content=f"错误：MCP 工具 {self.name} 调用失败：{e}",
                is_error=True,
            )

        texts, dropped = _collect_text(getattr(result, "content", None))
        if dropped and not self._warned_non_text:
            self._warned_non_text = True
            _warn(
                f"MCP 工具 {self.name} 返回了非文本内容块，已丢弃（每个工具只提示一次）"
            )

        content = "\n".join(texts)
        if getattr(result, "isError", False):
            return ToolResult(
                ok=False,
                content=content or f"错误：MCP 工具 {self.name} 返回错误",
                is_error=True,
            )
        return ToolResult(ok=True, content=content)


def _collect_text(content: Any) -> tuple[list[str], bool]:
    """收集 TextContent 的文本；返回 (文本列表, 是否丢弃过非文本块)。"""
    texts: list[str] = []
    dropped = False
    for block in content or []:
        if getattr(block, "type", None) == "text" and isinstance(
            getattr(block, "text", None), str
        ):
            texts.append(block.text)
        else:
            dropped = True
    return texts, dropped


def _read_only(tool: Any) -> bool:
    """只有 readOnlyHint 严格为 True 才算只读；其余一律按有副作用处理。"""
    annotations = getattr(tool, "annotations", None)
    return getattr(annotations, "readOnlyHint", None) is True


def build_remote_tools(server: str, session: Any, tools: list[Any]) -> list[RemoteTool]:
    """把一个 server 列出的工具全部包装好；单条不合法只跳过它自己。"""
    result: list[RemoteTool] = []
    for t in tools:
        remote_name = getattr(t, "name", "")
        name = f"mcp__{server}__{remote_name}"
        if not _NAME_RE.match(name):
            _warn(f"MCP 工具名含非法字符，已跳过: {name}")
            continue

        description = (getattr(t, "description", None) or "").strip()
        if not description:
            description = f"{server} 提供的工具 {remote_name}"

        schema = getattr(t, "inputSchema", None)
        if not isinstance(schema, dict) or not schema:
            schema = {"type": "object", "properties": {}}

        result.append(
            RemoteTool(
                server=server,
                remote_name=remote_name,
                name=name,
                description=description,
                parameters=schema,
                read_only=_read_only(t),
                session=session,
            )
        )
    return result
