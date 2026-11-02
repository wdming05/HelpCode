"""斜杠输入的纯函数解析器。"""

from __future__ import annotations

from .types import ParsedCommand


def parse(text: str) -> ParsedCommand | None:
    """把一行输入切分成命令名与参数；不是命令返回 ``None``。

    规则：
    - 空串 / 纯空白 / 不以 ``/`` 开头 → ``None``（不是命令）
    - 命令名 = 第一个空白之前的部分，转小写；参数保留中间空格
    - 只有一个 ``/``（名称为空）→ 返回 ``name=""``，由分发器按未知命令处理
    """
    if not text or not text.strip():
        return None
    if not text.startswith("/"):
        return None

    body = text[1:]
    parts = body.split(None, 1)
    name = parts[0].lower() if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""
    return ParsedCommand(name=name, args=args, raw=text)
