"""读文件工具。"""

from . import ToolResult
from .paths import resolve_path


class ReadFileTool:
    """读取文件内容（带行号），支持 offset/limit 与截断。"""

    name = "read_file"
    read_only = True
    # ch14：支持显式工作目录（cwd 不是模型可见参数，由 Agent 注入）
    cwd_aware = True
    # 落盘豁免：本工具的结果若被落盘，预览里会给出落盘路径，模型会再用
    # read_file 把那个文件读回来 —— 读回来的内容照样超阈值 → 再落盘，
    # 形成「落盘 → 读回 → 再落盘」的死循环。豁免后由下面的字符守卫兜底。
    # 生效处：context/constants.py 的 SPILL_EXEMPT_TOOLS。
    spillable = False

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

    # 内部守卫（豁免落盘的前提）：单次返回必须自己保证塞不满上下文。
    # 单行上限挡住 minified JSON / 压缩代码这类「一行几十万字符」；
    # 总量上限挡住「limit 传了 100000」这类滥用。
    MAX_LINE_CHARS = 2000
    MAX_TOTAL_CHARS = 100_000

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
        if not selected:
            return ToolResult(
                ok=False, content=f"错误：offset {start} 超出文件总行数 {total}"
            )

        numbered_lines: list[str] = []
        used = 0
        long_line_cut = False
        for i, line in enumerate(selected, start=start):
            if len(line) > self.MAX_LINE_CHARS:
                long_line_cut = True
                original = len(line)
                line = line[: self.MAX_LINE_CHARS] + (
                    f"…（该行原有 {original} 字符，已截断）"
                )
            entry = f"{i}: {line}"
            # 第一行无论多长都收下，否则 offset 对不上、模型会以为文件是空的
            if numbered_lines and used + len(entry) > self.MAX_TOTAL_CHARS:
                break
            numbered_lines.append(entry)
            used += len(entry) + 1

        numbered = "\n".join(numbered_lines)
        shown_end = start + len(numbered_lines) - 1
        if long_line_cut:
            numbered += f"\n(注意：超过 {self.MAX_LINE_CHARS} 字符的行已截断)"
        if shown_end < total:
            numbered += (
                f"\n(已截断：共 {total} 行，本次返回第 {start}-{shown_end} 行；"
                f"继续请用 offset={shown_end + 1})"
            )
        return ToolResult(ok=True, content=numbered)
