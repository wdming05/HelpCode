"""渲染拼装：状态栏、消息块、错误样式、markdown 定型、工具行。"""

import json

from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from ..llm import TokenUsage, ToolCall
from ..permission import Mode
from ..tool import ToolResult


def user_block(text: str) -> Text:
    """用户输入块：加粗青色前缀 ●，带显式颜色确保各终端可见。"""
    return Text("● " + text, style="bold cyan")


def render_markdown(reply: str) -> Markdown:
    """把助手回复渲染为 markdown（流式结束后定型）。"""
    return Markdown(reply)


def error_block(err: Exception) -> Text:
    """错误块：红色可区分样式。"""
    return Text("● 错误: " + str(err), style="bold red")


_MODE_STYLE = {
    Mode.DEFAULT: "green",
    Mode.ACCEPT_EDITS: "cyan",
    Mode.PLAN: "yellow",
    Mode.BYPASS: "red",
}


def mode_mark(mode: Mode) -> str:
    """权限模式的状态栏标记，如 ``[PLAN]``。"""
    return f"[{mode.name}]"


def render_statusbar(mode: Mode, model: str) -> Table:
    """状态栏：左权限模式标记（如 [PLAN]）、右模型名，两端对齐。"""
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1, justify="left")
    grid.add_column(ratio=1, justify="right")
    style = _MODE_STYLE.get(mode, "")
    grid.add_row(Text(mode_mark(mode), style=f"bold {style}"), Text(model))
    return grid


def render_permission_request(req, cursor: int = 0) -> Text:
    """渲染待批准确认块：动作名 + 参数 + 原因 + 三选项菜单。"""
    lines = [
        Text(f"● {req.friendly_name}({req.preview})", style="bold cyan"),
        Text(f"  {req.reason}", style="dim"),
        Text("是否继续?", style="bold"),
    ]
    options = ["允许本次", "永久放行", "拒绝本次"]
    for idx, opt in enumerate(options):
        prefix = ">" if idx == cursor else " "
        style = "bold" if idx == cursor else ""
        lines.append(Text(f"  {prefix} {opt}", style=style))
    lines.append(Text("↑↓ 选择 · 回车确认 · Esc 取消", style="dim"))
    return Text("\n").join(lines)


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
    """Token 用量展示（一轮结束定型时入对话区）。

    缓存字段为 0 时不渲染（OpenAI 兼容端点的 cache_write 恒为 0，
    不返回缓存统计的端点 cache_read 也为 0），避免噪音。
    """
    text = f"（{usage.input_tokens} 输入 / {usage.output_tokens} 输出 tokens"
    if usage.cache_write:
        text += f" / 缓存写入 {usage.cache_write}"
    if usage.cache_read:
        text += f" / 缓存命中 {usage.cache_read}"
    return Text(text + "）", style="dim")


def render_compress_start(info) -> Text:
    """压缩开始（动态区）：先告诉用户正在忙，而不是干等。"""
    return Text(
        f"正在压缩上下文…（当前约 {info.tokens_before:,} tokens）", style="dim italic"
    )


def render_compress_done(info) -> Text:
    """压缩完成（日志行）：带前后 token，用户才能判断这次压缩值不值。"""
    return Text(
        f"已压缩：摘要 {info.summarized} 条 / 保留 {info.kept} 条，"
        f"tokens {info.tokens_before:,} → {info.tokens_after:,}",
        style="dim",
    )


def render_iteration(n: int) -> Text:
    """迭代轮次展示（流式态动态区）。"""
    return Text(f"第 {n}/25 轮", style="bold")


def render_notice(text: str) -> Text:
    """停止提示块（黄色，区别于错误红色）。"""
    return Text("● " + text, style="bold yellow")


# ---------------------------------------------------------------- 会话恢复（ch09）


def _ago(seconds: float) -> str:
    """相对时间：刚 / N 分钟前 / N 小时前 / 昨天 / N 天前。

    用 ID 里的创建时间算，不用文件 mtime——mtime 会被复制、备份、云同步重写。
    """
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    days = int(seconds // 86400)
    if days == 1:
        return "昨天"
    return f"{days} 天前"


def render_session_option(meta, now: float) -> Text:
    """会话列表的一行：标题 · 相对时间 · 模型 · 大小 · 条数（AC37）。"""
    size_kb = meta.size_bytes / 1024
    line = Text()
    line.append(meta.title or "（无标题）", style="bold")
    line.append(f"　{_ago(max(0.0, now - meta.updated))}", style="dim")
    line.append(f"　{meta.model}", style="cyan")
    line.append(f"　{size_kb:.1f} KB", style="dim")
    line.append(f"　{meta.messages} 条", style="dim")
    return line


def render_restored(name: str, messages: int) -> Text:
    """恢复成功提示行（AC34）。"""
    return Text(f"已恢复会话 {name}：{messages} 条消息", style="dim")


def render_repaired(summary: str) -> Text:
    """修复汇总行；空串返回空 Text（调用方据此决定写不写）。"""
    return Text(summary, style="yellow") if summary else Text()


def render_cleanup(line: str) -> Text:
    """清理汇总行；空串返回空 Text。"""
    return Text(line, style="dim") if line else Text()
