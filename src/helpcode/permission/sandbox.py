"""路径沙箱：文件类工具的路径围栏。

已知盲区：glob/grep 的越界遍历由工具内部 ``Path.walk``/``os.walk``
（不跟随目录软链接）限制；沙箱只围栏「搜索根」，不逐个校验命中文件。
"""

from pathlib import Path

# 文件类工具（走沙箱）
_FILE_TOOLS = ("read_file", "write_file", "edit_file", "glob", "grep")


def inside(root: str, target: str) -> tuple[bool, str]:
    """判断 target 是否落在 root 内（先解析符号链接再前缀判断）。"""
    try:
        root_resolved = Path(root).resolve()
        p = Path(target)
        if not p.is_absolute():
            p = root_resolved / p
        resolved = p.resolve()
    except (OSError, ValueError):
        return False, f"路径无法解析：{target}"
    try:
        resolved.relative_to(root_resolved)
        return True, ""
    except ValueError:
        return False, f"路径在项目目录之外：{target}"


def extract_target(call) -> tuple[str, bool, bool]:
    """提取调用目标，返回 (target, is_file, ok)。

    - 文件类：取 ``input["path"]``（glob/grep 空→"."），is_file=True
    - bash：取 ``input["command"]``（缺视为空串），is_file=False
    - 未知工具：target=""，is_file=False
    """
    name = call.name
    inp = call.input or {}
    if name in _FILE_TOOLS:
        path = inp.get("path")
        if path is None:
            # glob/grep 无 path 字段时用搜索根 "."
            if name in ("glob", "grep"):
                return ".", True, True
            return "", True, False  # 文件类缺 path → 不可解析
        if not isinstance(path, str) or not path.strip():
            return "", True, False
        return path.strip(), True, True
    if name == "bash":
        command = inp.get("command")
        if not isinstance(command, str):
            command = ""
        return command, False, True
    return "", False, True
