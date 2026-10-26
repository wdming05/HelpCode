"""写文件工具。"""

from . import ToolResult
from .paths import resolve_path


class WriteFileTool:
    """写入（覆盖）文件，父目录不存在时自动创建。"""

    name = "write_file"
    read_only = False
    cwd_aware = True  # ch14：支持显式工作目录
    description = "把内容写入指定文件（覆盖原有内容）；父目录不存在时会自动创建。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要写入的文件路径"},
            "content": {"type": "string", "description": "要写入的完整内容"},
        },
        "required": ["path", "content"],
    }

    def run(self, path: str, content: str, cwd: str | None = None) -> ToolResult:
        try:
            p = resolve_path(path, cwd)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except Exception as e:
            return ToolResult(ok=False, content=f"错误：写入文件失败 {e}")
        return ToolResult(ok=True, content=f"写入成功：{path}")
