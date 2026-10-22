"""搜代码内容工具。"""

from pathlib import Path

from . import ToolResult

# 命中条数上限
_RESULT_LIMIT = 200


class GrepTool:
    """在文件内容中搜索模式，返回命中位置（文件/行号/内容）。"""

    name = "grep"
    read_only = True
    description = (
        "在文件内容中搜索指定文本，返回命中位置（文件路径、行号、该行内容）。"
        "path 可指定单个文件或目录（默认当前目录，递归搜索）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "要搜索的文本"},
            "path": {"type": "string", "description": "搜索范围（文件或目录），默认 ."},
        },
        "required": ["pattern"],
    }

    def run(self, pattern: str, path: str = ".") -> ToolResult:
        p = Path(path)
        if not p.exists():
            return ToolResult(ok=False, content=f"错误：路径不存在 {path}")

        if p.is_file():
            files = [p]
        else:
            files = [f for f in p.rglob("*") if f.is_file()]

        hits = []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):  # 跳过二进制/不可读文件
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    hits.append(f"{f}:{lineno}: {line}")
                    if len(hits) >= _RESULT_LIMIT:
                        break
            if len(hits) >= _RESULT_LIMIT:
                break

        if not hits:
            return ToolResult(ok=False, content=f"未找到匹配：{pattern}")

        result = "\n".join(hits)
        if len(hits) >= _RESULT_LIMIT:
            result += f"\n(已截断：命中超过 {_RESULT_LIMIT} 条)"
        return ToolResult(ok=True, content=result)
