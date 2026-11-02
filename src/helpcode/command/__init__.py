"""命令系统：注册中心 + 解析器 + 内置命令的统一出口。"""

from .builtin import BUILTINS, build_default_registry
from .host import CommandHost, HostSnapshot, MemoryOverview
from .parser import parse
from .registry import CommandRegistry, DuplicateCommandError
from .types import (
    Command,
    CommandContext,
    CommandType,
    Handler,
    ParsedCommand,
)

__all__ = [
    "BUILTINS",
    "Command",
    "CommandContext",
    "CommandHost",
    "CommandRegistry",
    "CommandType",
    "DuplicateCommandError",
    "Handler",
    "HostSnapshot",
    "MemoryOverview",
    "ParsedCommand",
    "build_default_registry",
    "parse",
]
