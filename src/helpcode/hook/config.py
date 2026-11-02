"""hooks.yaml 的加载与校验。

两层叠加：项目 ``<root>/.helpcode/hooks.yaml`` + 用户 ``~/.helpcode/hooks.yaml``。
不存在「覆盖」——两条都参与分派；同名时后到者跳过并告警（F7）。
任何单条错误只跳过该条并告警，绝不阻断启动（N2）。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ..permission.pattern import PatternError, compile_pattern
from .events import INTERCEPT_EVENTS, HookEvent, parse_event
from .schema import (
    ACTION_TYPES,
    COMMAND,
    HTTP,
    PROMPT,
    SUBAGENT,
    Action,
    Condition,
    ConditionGroup,
    Hook,
)

PROJECT_FILE = Path(".helpcode") / "hooks.yaml"
USER_FILE = Path(".helpcode") / "hooks.yaml"
DEFAULT_TIMEOUT = 30.0

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m)?$")


class _Bad(ValueError):
    """单条 Hook 非法（内部用，转成告警文本）。"""


def load_hooks(root: Path, home: Path | None = None) -> tuple[list[Hook], list[str]]:
    """加载两层 Hook，返回 (hooks, warnings)。"""
    home = home if home is not None else Path.home()
    root = root.resolve()
    paths = [root / PROJECT_FILE, home / USER_FILE]

    hooks: list[Hook] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for path in paths:
        for item in _load_file(path, warnings):
            try:
                hook = _parse_hook(item)
            except _Bad as e:
                warnings.append(f"{path}: {e}，已跳过")
                continue
            if hook.event in INTERCEPT_EVENTS and hook.is_async:
                warnings.append(
                    f"{path}: Hook「{hook.name}」是拦截事件 {hook.event.value}，"
                    "不允许 async，已跳过"
                )
                continue
            if hook.name in seen:
                warnings.append(
                    f"{path}: Hook 名「{hook.name}」与前面重复，已跳过后到者"
                )
                continue
            seen.add(hook.name)
            hooks.append(hook)

    return hooks, warnings


def _load_file(path: Path, warnings: list[str]) -> list[dict]:
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as e:
        warnings.append(f"{path}: 读取/解析失败（{e}），已跳过")
        return []
    if raw is None:
        return []
    if not isinstance(raw, dict):
        warnings.append(f"{path}: 顶层必须是 mapping，已跳过")
        return []
    items = raw.get("hooks")
    if items is None:
        return []
    if not isinstance(items, list):
        warnings.append(f"{path}: hooks 必须是数组，已跳过")
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            warnings.append(f"{path}: hooks 里混入非对象条目，已跳过")
            continue
        out.append(item)
    return out


def _parse_hook(item: dict) -> Hook:
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _Bad("缺 name")

    event_name = item.get("event")
    if not isinstance(event_name, str):
        raise _Bad(f"Hook「{name}」缺 event")
    event = parse_event(event_name)
    if event is None:
        raise _Bad(f"Hook「{name}」的事件名非法：{event_name}")

    action = _parse_action(item.get("action"), name)
    condition = _parse_condition(item.get("if"), name)

    return Hook(
        name=name.strip(),
        event=event,
        condition=condition,
        action=action,
        only_once=bool(item.get("only_once", False)),
        is_async=bool(item.get("async", False)),
        timeout=_parse_timeout(item.get("timeout")),
    )


def _parse_action(raw, name: str) -> Action:
    if not isinstance(raw, dict):
        raise _Bad(f"Hook「{name}」缺 action")
    typ = raw.get("type")
    if typ not in ACTION_TYPES:
        raise _Bad(f"Hook「{name}」的动作类型非法：{typ}")
    if typ == COMMAND:
        cmd = raw.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise _Bad(f"Hook「{name}」的 command 动作缺 command")
        return Action(COMMAND, command=cmd)
    if typ == PROMPT:
        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise _Bad(f"Hook「{name}」的 prompt 动作缺 prompt")
        return Action(PROMPT, prompt=prompt)
    if typ == HTTP:
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            raise _Bad(f"Hook「{name}」的 http 动作缺 url")
        body = raw.get("body") if isinstance(raw.get("body"), dict) else None
        headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else None
        return Action(
            HTTP,
            url=url.strip(),
            method=str(raw.get("method", "POST")).upper(),
            body=body,
            headers=headers,
        )
    # subagent：占位，只保留 prompt 供日志
    return Action(SUBAGENT, prompt=str(raw.get("prompt", "")))


def _parse_condition(raw, name: str) -> ConditionGroup | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _Bad(f"Hook「{name}」的 if 必须是 mapping")

    has_all = "all_of" in raw
    has_any = "any_of" in raw
    if has_all and has_any:
        raise _Bad(f"Hook「{name}」的 if 同时用了 all_of 与 any_of")

    if has_all or has_any:
        mode = "all" if has_all else "any"
        items = raw.get("all_of" if has_all else "any_of")
        if not isinstance(items, list) or not items:
            raise _Bad(f"Hook「{name}」的 {mode}_of 必须是非空数组")
        return ConditionGroup(mode, tuple(_condition_one(i, name) for i in items))

    return ConditionGroup("all", (_condition_one(raw, name),))


def _condition_one(raw, name: str) -> Condition:
    if not isinstance(raw, dict):
        raise _Bad(f"Hook「{name}」的条件必须是 mapping")
    field = raw.get("field")
    pattern = raw.get("pattern")
    if not isinstance(field, str) or not field.strip():
        raise _Bad(f"Hook「{name}」的条件缺 field")
    if not isinstance(pattern, str):
        raise _Bad(f"Hook「{name}」的条件缺 pattern")
    try:
        matcher = compile_pattern(pattern)
    except PatternError as e:
        raise _Bad(f"Hook「{name}」的条件模式非法：{e}") from e
    return Condition(field.strip(), matcher)


def _parse_timeout(raw) -> float:
    """``"30s"`` / ``"500ms"`` / ``"2m"`` → 秒；非法回落默认值。"""
    if raw is None:
        return DEFAULT_TIMEOUT
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw) if raw > 0 else DEFAULT_TIMEOUT
    if isinstance(raw, str):
        m = _DURATION.match(raw.strip().lower())
        if m is None:
            return DEFAULT_TIMEOUT
        num = float(m.group(1))
        unit = m.group(2) or "s"
        if unit == "ms":
            return num / 1000.0
        if unit == "m":
            return num * 60.0
        return num
    return DEFAULT_TIMEOUT


__all__ = ["HookEvent", "load_hooks"]
