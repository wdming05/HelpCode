"""命令的类型与数据结构。本模块不依赖任何 UI 库。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .host import CommandHost
    from .registry import CommandRegistry


class CommandType(Enum):
    """命令的执行模式，决定分发器对返回值的处理语义。"""

    LOCAL = "local"  # 命令自己做完，不进入 AI 对话
    UI_STATE = "ui_state"  # 改变界面/会话状态，不触发模型请求
    PROMPT = "prompt"  # 把预设提示词当作用户消息送进对话


@dataclass(frozen=True)
class Command:
    """一条命令的完整元数据。

    ``name`` 与 ``aliases`` 都不带斜杠、统一小写；两者共享注册命名空间，
    任何一个键重复都会在注册时被拒绝。
    """

    name: str
    description: str
    usage: str
    type: CommandType
    handler: Handler
    aliases: tuple[str, ...] = ()
    arg_hint: str = ""
    hidden: bool = False

    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class ParsedCommand:
    """解析产物。``name`` 已小写；``args`` 只去首尾空白、保留中间空格。"""

    name: str
    args: str
    raw: str


@dataclass
class CommandContext:
    """一次命令执行的全部上下文。"""

    command: Command
    args: str
    host: CommandHost
    registry: CommandRegistry


Handler = Callable[[CommandContext], Awaitable[None]]
