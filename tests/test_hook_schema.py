"""Hook schema：条件组合、payload 取值、动作构造。"""

from helpcode.hook.events import HookEvent
from helpcode.hook.schema import (
    Action,
    Condition,
    ConditionGroup,
    Hook,
    HookResult,
    field_to_text,
)
from helpcode.permission.pattern import compile_pattern


def _cond(field, pattern):
    return Condition(field, compile_pattern(pattern))


def test_all_of():
    g = ConditionGroup("all", (_cond("tool", "=bash"), _cond("input.command", "~^rm")))
    assert g.matches({"tool": "bash", "input": {"command": "rm -rf x"}})
    assert not g.matches({"tool": "bash", "input": {"command": "ls"}})


def test_any_of():
    g = ConditionGroup("any", (_cond("tool", "=bash"), _cond("input.command", "~^rm")))
    assert g.matches({"tool": "bash", "input": {"command": "ls"}})
    assert g.matches({"tool": "read_file", "input": {"command": "rm x"}})
    assert not g.matches({"tool": "read_file", "input": {"command": "ls"}})


def test_field_to_text():
    assert field_to_text({"tool": "bash"}, "tool") == "bash"
    assert field_to_text({"input": {"command": "ls"}}, "input.command") == "ls"
    assert field_to_text({"a": True}, "a") == "true"
    assert field_to_text({"a": False}, "a") == "false"
    assert field_to_text({}, "missing.path") == ""


def test_hook_construction():
    hook = Hook(
        name="x",
        event=HookEvent.PRE_TOOL_USE,
        condition=None,
        action=Action("prompt", prompt="hi"),
        only_once=True,
        is_async=False,
        timeout=5.0,
    )
    assert hook.name == "x" and hook.only_once and hook.timeout == 5.0


def test_hook_result_defaults():
    r = HookResult()
    assert r.block_reason is None and r.injected == []
