"""读文件工具。"""

from . import ToolResult
from .paths import resolve_path


class ReadFileTool:
    """读取文件内容（带行号），支持 offset/limit 与截断。"""

    name = "read_file"
    read_only = True
    # ch14：支持显式工作目录（cwd 不是模型可见参数，由 Agent 注入）
    cwd_aware = True
    description = (
        "读取指定文件的内容，返回带行号的文本，便于引用。"
        "可指定起始行 offset 与读取行数 limit；文件过长时会截断并提示。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要读取的文件路径"},
            "offset": {
                "type": "integer",
                "description": "起始行号（从 1 开始），默认 1",
            },
            "limit": {"type": "integer", "description": "读取行数，默认 200"},
        },
        "required": ["path"],
    }

    def run(
        self,
        path: str,
        offset: int = 1,
        limit: int = 200,
        cwd: str | None = None,
    ) -> ToolResult:
        p = resolve_path(path, cwd)
        if not p.exists():
            return ToolResult(ok=False, content=f"错误：文件不存在 {path}")
        if p.is_dir():
            return ToolResult(ok=False, content=f"错误：{path} 是目录，不是文件")
        try:
            # read_text(encoding="utf-8") : 按 UTF-8 编码把整个文件读成一个字符串
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            return ToolResult(ok=False, content=f"错误：读取文件失败 {e}")

        total = len(lines)
        start = max(offset, 1)
        end = start + max(limit, 1) - 1
        selected = lines[start - 1 : end]

        numbered = "\n".join(f"{i + start}: {line}" for i, line in enumerate(selected))
        if end < total:
            numbered += f"\n(已截断：共 {total} 行，仅显示第 {start}-{end} 行)"
        return ToolResult(ok=True, content=numbered)
