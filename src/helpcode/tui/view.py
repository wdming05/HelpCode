"""渲染拼装：状态栏、消息块、错误样式、markdown 定型、工具行。"""

import json

from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from ..llm import TokenUsage, ToolCall
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


def render_tool_start(tool_call: ToolCall) -> Text:
    """工具调用开始：● 工具名(关键参数)。"""
    args = _summarize_args(tool_call.input)
    return Text(f"● {tool_call.name}({args})", style="bold cyan")


def render_tool_end(tool_call: ToolCall, tool_result: ToolResult) -> Text:
    """工具调用结束：结果摘要；失败红色、过长截断。"""
    summary = tool_result.content
    if len(summary) > 120:
        summary = summary[:120] + "…"
    return Text(summary, style="" if tool_result.ok else "red")


def render_token_usage(usage: TokenUsage) -> Text:
    """Token 用量展示（一轮结束定型时入对话区）。"""
    return Text(
        f"（{usage.input_tokens} 输入 / {usage.output_tokens} 输出 tokens）",
        style="dim",
    )


def render_iteration(n: int) -> Text:
    """迭代轮次展示（流式态动态区）。"""
    return Text(f"第 {n}/25 轮", style="bold")


def render_notice(text: str) -> Text:
    """停止提示块（黄色，区别于错误红色）。"""
    return Text("● " + text, style="bold yellow")
