"""渲染拼装：状态栏、消息块、错误样式、markdown 定型。"""

from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text


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
