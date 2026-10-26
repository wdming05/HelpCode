"""改文件工具。"""

from . import ToolResult
from .paths import resolve_path


class EditFileTool:
    """对文件中的原文片段做唯一匹配替换。"""

    name = "edit_file"
    read_only = False
    cwd_aware = True  # ch14：支持显式工作目录
    description = (
        "把文件中「old_string」这段原文替换成「new_string」。"
        "要求 old_string 在文件中恰好出现一次；出现 0 次或多次会报错。"
        "编辑前请先用 read_file 读取目标文件，确认 old_string 唯一。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要修改的文件路径"},
            "old_string": {"type": "string", "description": "要替换的原文片段"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def run(
        self,
        path: str,
        old_string: str,
        new_string: str,
        cwd: str | None = None,
    ) -> ToolResult:
        p = resolve_path(path, cwd)
        if not p.exists():
            return ToolResult(ok=False, content=f"错误：文件不存在 {path}")
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(ok=False, content=f"错误：读取文件失败 {e}")

        count = text.count(old_string)
        if count == 0:
            return ToolResult(
                ok=False, content="错误：未找到要替换的原文片段（匹配 0 次）"
            )
        if count > 1:
            return ToolResult(
                ok=False, content=f"错误：原文片段匹配了 {count} 次，需唯一匹配才能替换"
            )

        try:
            p.write_text(text.replace(old_string, new_string), encoding="utf-8")
        except Exception as e:
            return ToolResult(ok=False, content=f"错误：写入文件失败 {e}")
        return ToolResult(ok=True, content=f"修改成功：{path}")
