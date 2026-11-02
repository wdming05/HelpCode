"""Hook 生命周期事件枚举。"""

from __future__ import annotations

from enum import Enum


class HookEvent(Enum):
    """11 个生命周期事件（值即 YAML 里写的事件名）。"""

    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    SESSION_RESUME = "SessionResume"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    STOP = "Stop"
    PRE_USER_MESSAGE = "PreUserMessage"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    NOTIFICATION = "Notification"


# 可拦截事件：同步 shell 动作 exit 2 表示拦截。这些事件不允许 async（F15）。
INTERCEPT_EVENTS = frozenset({HookEvent.USER_PROMPT_SUBMIT, HookEvent.PRE_TOOL_USE})


def parse_event(value: str) -> HookEvent | None:
    """事件名 → 枚举；未知返回 None。大小写敏感（与 YAML 写法一致）。"""
    for event in HookEvent:
        if event.value == value:
            return event
    return None
