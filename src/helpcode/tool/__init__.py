"""工具抽象与注册中心。"""

from dataclasses import dataclass
from typing import Any, Protocol

# 命令执行默认超时（秒）
DEFAULT_TIMEOUT = 30.0


@dataclass
class ToolResult:
    """工具执行的结构化结果：成功内容或错误信息。"""

    ok: bool
    content: str


def _truncate(text: str, limit: int) -> str:
    """截断长文本；超出 limit 时截断并附加「(已截断)」提示。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n(已截断，共 {len(text)} 字符)"


class Tool(Protocol):
    """工具协议：每个工具是实现 name/description/parameters + run() 的对象。"""

    name: str
    description: str
    parameters: dict
    read_only: bool  # True=只读可并发，False=有副作用串行

    def run(self, **kwargs: Any) -> ToolResult: ...


class Registry:
    """工具注册中心：登记工具、按名查找、导出统一工具定义。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def export(self) -> list[dict]:
        """导出统一格式的工具定义（各协议适配器内部再转成自身格式）。"""
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self._tools.values()
        ]

    def read_only_definitions(self) -> list[dict]:
        """只导出只读工具的定义（Plan Mode 用）。"""
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self._tools.values()
            if t.read_only
        ]

    def is_read_only(self, name: str) -> bool:
        """判断某工具是否只读；未知工具返回 False。"""
        tool = self._tools.get(name)
        return tool.read_only if tool is not None else False


def new_default_registry() -> Registry:
    """注册全部 6 个工具，返回注册中心。"""
    # 延迟 import，避免与各工具文件形成循环导入
    from .bash import BashTool
    from .edit_file import EditFileTool
    from .glob_tool import GlobTool
    from .grep_tool import GrepTool
    from .read_file import ReadFileTool
    from .write_file import WriteFileTool

    registry = Registry()
    for tool in (
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        BashTool(),
        GlobTool(),
        GrepTool(),
    ):
        registry.register(tool)
    return registry
