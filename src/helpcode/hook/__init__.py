"""Hook 生命周期挂钩系统：事件、规则、引擎与四种动作。"""

from .actions import run_action, subst
from .config import load_hooks
from .engine import HookEngine
from .events import INTERCEPT_EVENTS, HookEvent, parse_event
from .schema import (
    ACTION_TYPES,
    Action,
    Condition,
    ConditionGroup,
    Hook,
    HookResult,
)

__all__ = [
    "ACTION_TYPES",
    "INTERCEPT_EVENTS",
    "Action",
    "Condition",
    "ConditionGroup",
    "Hook",
    "HookEngine",
    "HookEvent",
    "HookResult",
    "load_hooks",
    "parse_event",
    "run_action",
    "subst",
]
