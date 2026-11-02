"""压缩后的恢复段：已改文件清单 + 当前工具集 + 边界提示。

摘要会丢掉具体代码细节，模型容易「照着摘要脑补代码」。这一段用极低的成本
（约 100 token）把最不可推断的两件事补回去：改过哪些文件、现在手上有哪些工具。

不写文件内容快照：那会把刚省下的 token 又塞回去，而落盘文件已能按需读取。
"""

from ..llm import Message

# 会改动磁盘的工具（glob / grep / read_file / bash 都不算）
WRITE_TOOLS = ("write_file", "edit_file")

BOUNDARY_HINT = (
    "如需以上文件或其它文件的确切内容，请用 read_file 重新读取，不要依据摘要推断代码。"
)


def build_resume(chunk_messages: list[Message], tool_names: list[str]) -> str:
    """构造恢复段文本；``chunk_messages`` 是被摘要掉的那段历史。"""
    files: list[str] = []
    seen: set[str] = set()
    for m in chunk_messages:
        if m.role != "assistant" or not m.tool_calls:
            continue
        for tc in m.tool_calls:
            if tc.name not in WRITE_TOOLS:
                continue
            path = tc.input.get("path") if isinstance(tc.input, dict) else None
            if path and path not in seen:
                seen.add(path)
                files.append(str(path))

    lines: list[str] = []
    if files:
        lines.append("本次会话已写入/修改的文件：")
        lines.extend(f"- {p}" for p in files)
    if tool_names:
        lines.append("当前可用工具：" + " / ".join(tool_names))
    lines.append(BOUNDARY_HINT)
    return "\n".join(lines)
