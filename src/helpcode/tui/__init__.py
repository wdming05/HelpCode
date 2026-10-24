"""TUI 包。"""

from enum import Enum


class SessionState(Enum):
    """会话状态机：选择 / 空闲 / 流式 / 待批准 / 选会话。"""

    SELECTING = "selecting"
    IDLE = "idle"
    STREAMING = "streaming"
    APPROVING = "approving"
    RESUMING = "resuming"
