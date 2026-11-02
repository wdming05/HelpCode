"""命令宿主协议与只读快照。命令实现只依赖这里，不依赖任何 UI 库。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..llm import TokenUsage
from ..memory.session import SessionMeta
from ..permission import Mode


@dataclass(frozen=True)
class MemoryOverview:
    """`/memory` 需要展示的记忆状态只读快照。"""

    layers: tuple[tuple[str, bool], ...] = ()  # (层名, 是否命中)
    index_lines: tuple[tuple[str, int], ...] = ()  # (层名, 注入行数)
    note_count: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostSnapshot:
    """一次命令执行期间保持不变的宿主只读快照（F10）。"""

    model: str = ""
    session_id: str | None = None
    message_count: int = 0
    context_window: int = 0
    context_used: int = 0
    mode: Mode = Mode.DEFAULT
    usage: TokenUsage | None = None
    sessions: tuple[SessionMeta, ...] = ()
    memory: MemoryOverview = MemoryOverview()


class CommandHost(Protocol):
    """命令与外界交互的唯一通道。实现方（如 TUI）自行决定怎么渲染。"""

    def snapshot(self) -> HostSnapshot: ...

    def show(self, text: str) -> None: ...

    def show_error(self, text: str) -> None: ...

    def clear_screen(self) -> None: ...

    async def send_prompt(self, text: str) -> None: ...

    async def run_skill(self, name: str, args: str) -> str: ...

    async def compact(self) -> str: ...

    def get_mode(self) -> Mode: ...

    def set_mode(self, mode: Mode) -> None: ...

    def refresh_status(self) -> None: ...

    async def start_new_session(self) -> str | None: ...

    def open_session_picker(self) -> None: ...

    async def worktree(self, sub: str, args: str) -> str:
        """ch14：Worktree 子命令入口，返回要展示的文本（失败也用文本表达）。"""
        ...

    async def team(self, sub: str, args: str) -> str:
        """ch15：Team 子命令入口，同样只用文本表达结果。"""
        ...

    async def quit(self) -> None: ...
