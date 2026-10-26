"""按模式找文件工具。"""

import glob as _glob
import os

from . import ToolResult

# 匹配结果条数上限
_RESULT_LIMIT = 200


class GlobTool:
    """按 glob 模式查找文件，返回匹配路径列表。"""

    name = "glob"
    read_only = True
    cwd_aware = True  # ch14：支持显式工作目录
    description = "按 glob 模式查找文件，返回匹配的文件路径列表。支持 ** 递归匹配。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式，如 **/*.py"},
        },
        "required": ["pattern"],
    }

    def run(self, pattern: str, cwd: str | None = None) -> ToolResult:
        # 模式本身没有 Path 语义（含 * ?），不能过 resolve_path：
        # 绝对模式原样用，相对模式拼到 cwd 上
        target = (
            pattern if os.path.isabs(pattern) or not cwd else os.path.join(cwd, pattern)
        )
        try:
            matches = _glob.glob(target, recursive=True)
        except Exception as e:
            return ToolResult(ok=False, content=f"错误：glob 模式无效 {e}")
        if not matches:
            return ToolResult(ok=False, content=f"未匹配到任何文件：{pattern}")

        if len(matches) > _RESULT_LIMIT:
            shown = "\n".join(matches[:_RESULT_LIMIT])
            return ToolResult(
                ok=True,
                content=f"{shown}\n(已截断：共 {len(matches)} 个，仅显示前 {_RESULT_LIMIT} 个)",
            )
        return ToolResult(ok=True, content="\n".join(matches))
