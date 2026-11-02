"""Hook 规则的数据结构，以及 payload 点路径取值。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..permission.pattern import Matcher
from .events import HookEvent

# 动作类型
COMMAND = "command"
PROMPT = "prompt"
HTTP = "http"
SUBAGENT = "subagent"
ACTION_TYPES = (COMMAND, PROMPT, HTTP, SUBAGENT)


@dataclass(frozen=True)
class Condition:
    """一个条件：对 payload 的某个点路径值套 matcher。"""

    field: str
    matcher: Matcher


@dataclass(frozen=True)
class ConditionGroup:
    """多条件组合：mode 只取 all / any，不允许嵌套混用。"""

    mode: str  # "all" / "any"
    conditions: tuple[Condition, ...]

    def matches(self, payload: dict) -> bool:
        results = [
            c.matcher.matches(field_to_text(payload, c.field)) for c in self.conditions
        ]
        if self.mode == "all":
            return all(results)
        return any(results)


@dataclass(frozen=True)
class Action:
    """动作定义。只填 type 对应的字段。"""

    type: str
    command: str = ""
    prompt: str = ""
    url: str = ""
    method: str = "POST"
    body: dict | None = None
    headers: dict | None = None


@dataclass(frozen=True)
class Hook:
    """一条 Hook：事件 + 条件（可空）+ 动作 + 执行控制。"""

    name: str
    event: HookEvent
    condition: ConditionGroup | None
    action: Action
    only_once: bool = False
    is_async: bool = False
    timeout: float = 30.0


@dataclass
class HookResult:
    """一次 dispatch 的汇总结果。

    - ``block_reason``：仅拦截事件（PreToolUse / UserPromptSubmit）可能非空
    - ``injected``：prompt 动作产出的注入文本（进下一轮系统提醒）
    """

    block_reason: str | None = None
    injected: list[str] = field(default_factory=list)


def field_to_text(payload: dict, field: str) -> str:
    """按点路径从 payload 取值并转成字符串。

    - 缺失 → 空串（匹配器对空串通常不命中，安全）
    - bool → "true"/"false"（便于用 ``=true`` 精确匹配）
    - dict/list → JSON 串
    """
    current: Any = payload
    for part in field.split("."):
        if isinstance(current, dict):
            current = current.get(part, "")
        else:
            current = ""
            break
    if isinstance(current, bool):
        return "true" if current else "false"
    if isinstance(current, (dict, list)):
        import json

        return json.dumps(current, ensure_ascii=False, sort_keys=True)
    if current is None:
        return ""
    return str(current)
