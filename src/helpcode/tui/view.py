"""渲染拼装：状态栏、消息块、错误样式、markdown 定型、工具行。"""

import json

from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from ..llm import ToolCall
from ..tool import ToolResult


def user_block(text: str) -> Text:
    """用户输入块：加粗前缀 ●。"""
    return Text("● " + text, style="bold")


def render_markdown(reply: str) -> Markdown:
    """把助手回复渲染为 markdown（流式结束后定型）。"""
    return Markdown(reply)


def error_block(err: Exception) -> Text:
    """错误块：红色可区分样式。"""
    return Text("● 错误: " + str(err), style="bold red")


def render_statusbar(name: str, model: str) -> Table:
    """状态栏：左 provider 名、右模型名，两端对齐。"""
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1, justify="left")
    grid.add_column(ratio=1, justify="right")
    grid.add_row(Text(name, style="bold"), Text(model))
    return grid


def _summarize_args(input: dict, limit: int = 40) -> str:
    """提取工具调用的关键参数做简要展示。"""
    for key in ("path", "command", "pattern"):
        if key in input:
            s = str(input[key])
            return s if len(s) <= limit else s[:limit] + "…"
    s = json.dumps(input, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


def render_tool_line(tool_call: ToolCall, tool_result: ToolResult) -> Text:
    """工具行：● 工具名(关键参数) + 结果摘要；失败红色、过长截断。"""
    args = _summarize_args(tool_call.input)
    head = Text(f"● {tool_call.name}({args})", style="bold cyan")
    summary = tool_result.content
    if len(summary) > 120:
        summary = summary[:120] + "…"
    body = Text(summary, style="" if tool_result.ok else "red")
    return Text.assemble(head, "\n", body)
