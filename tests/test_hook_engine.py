"""HookEngine：条件、only_once、async、timeout、动作。"""

import asyncio
import sys

from helpcode.hook import HookEngine, HookEvent
from helpcode.hook.schema import Action, Condition, ConditionGroup, Hook, HookResult
from helpcode.permission.pattern import compile_pattern


def _hook(
    name="h",
    event=HookEvent.NOTIFICATION,
    action=None,
    condition=None,
    only_once=False,
    is_async=False,
    timeout=30.0,
):
    return Hook(
        name=name,
        event=event,
        condition=condition,
        action=action or Action("prompt", prompt="x"),
        only_once=only_once,
        is_async=is_async,
        timeout=timeout,
    )


def _run(coro):
    return asyncio.run(coro)


def test_block_reason_returned():
    async def runner(hook, payload):
        return HookResult(block_reason="no")

    engine = HookEngine([_hook()], runner=runner)
    res = _run(engine.dispatch(HookEvent.NOTIFICATION, {}))
    assert res.block_reason == "no"


def test_injected_collected():
    async def runner(hook, payload):
        return HookResult(injected=[hook.name])

    engine = HookEngine([_hook("a"), _hook("b")], runner=runner)
    res = _run(engine.dispatch(HookEvent.NOTIFICATION, {}))
    assert res.injected == ["a", "b"]


def test_condition_filters():
    cond = ConditionGroup("all", (Condition("tool", compile_pattern("=bash")),))
    calls = []

    async def runner(hook, payload):
        calls.append(hook.name)
        return HookResult()

    engine = HookEngine([_hook("a", condition=cond)], runner=runner)
    _run(engine.dispatch(HookEvent.NOTIFICATION, {"tool": "grep"}))
    assert calls == []
    _run(engine.dispatch(HookEvent.NOTIFICATION, {"tool": "bash"}))
    assert calls == ["a"]


def test_only_once():
    calls = []

    async def runner(hook, payload):
        calls.append(1)
        return HookResult()

    engine = HookEngine([_hook(only_once=True)], runner=runner)
    _run(engine.dispatch(HookEvent.NOTIFICATION, {}))
    _run(engine.dispatch(HookEvent.NOTIFICATION, {}))
    assert calls == [1]


def test_reset_session():
    calls = []

    async def runner(hook, payload):
        calls.append(1)
        return HookResult()

    engine = HookEngine([_hook(only_once=True)], runner=runner)
    _run(engine.dispatch(HookEvent.NOTIFICATION, {}))
    engine.reset_session()
    _run(engine.dispatch(HookEvent.NOTIFICATION, {}))
    assert calls == [1, 1]


def test_async_nonblocking():
    async def runner(hook, payload):
        await asyncio.sleep(0.2)
        return HookResult()

    engine = HookEngine([_hook(is_async=True)], runner=runner)

    async def go():
        import time

        start = time.monotonic()
        await engine.dispatch(HookEvent.NOTIFICATION, {})
        elapsed = time.monotonic() - start
        await engine.drain()
        return elapsed

    assert _run(go()) < 0.1


def test_runner_exception_swallowed():
    async def runner(hook, payload):
        raise RuntimeError("boom")

    engine = HookEngine([_hook()], runner=runner)
    res = _run(engine.dispatch(HookEvent.NOTIFICATION, {}))
    assert res.block_reason is None


# ---------------------------------------------------------------- 真实动作


def test_command_exit2_blocks():
    hook = _hook(
        event=HookEvent.PRE_TOOL_USE,
        action=Action("command", command="echo blocked 1>&2 && exit 2"),
        timeout=5.0,
    )
    engine = HookEngine([hook])
    res = _run(engine.dispatch(HookEvent.PRE_TOOL_USE, {"tool": "bash"}))
    assert res.block_reason and "blocked" in res.block_reason


def test_command_exit0_allows():
    hook = _hook(
        event=HookEvent.PRE_TOOL_USE,
        action=Action("command", command="exit 0"),
        timeout=5.0,
    )
    engine = HookEngine([hook])
    res = _run(engine.dispatch(HookEvent.PRE_TOOL_USE, {}))
    assert res.block_reason is None


def test_command_exit1_no_block():
    hook = _hook(
        event=HookEvent.PRE_TOOL_USE,
        action=Action("command", command="exit 1"),
        timeout=5.0,
    )
    engine = HookEngine([hook])
    res = _run(engine.dispatch(HookEvent.PRE_TOOL_USE, {}))
    assert res.block_reason is None


def test_command_timeout():
    cmd = f'"{sys.executable}" -c "import time;time.sleep(5)"'
    hook = _hook(action=Action("command", command=cmd), timeout=0.3)
    engine = HookEngine([hook])

    async def go():
        import time

        start = time.monotonic()
        res = await engine.dispatch(HookEvent.NOTIFICATION, {})
        return res, time.monotonic() - start

    res, elapsed = _run(go())
    assert res.block_reason is None
    assert elapsed < 3


def test_prompt_action():
    hook = _hook(action=Action("prompt", prompt="记住：{{tool}}"))
    engine = HookEngine([hook])
    res = _run(engine.dispatch(HookEvent.NOTIFICATION, {"tool": "bash"}))
    assert res.injected == ["记住：bash"]


def test_subagent_placeholder():
    hook = _hook(action=Action("subagent", prompt="do it"))
    engine = HookEngine([hook])
    res = _run(engine.dispatch(HookEvent.NOTIFICATION, {}))
    assert res.block_reason is None and res.injected == []


def test_http_failure_ignored():
    hook = _hook(
        action=Action("http", url="http://127.0.0.1:1/never"),
        timeout=1.0,
    )
    engine = HookEngine([hook])
    res = _run(engine.dispatch(HookEvent.NOTIFICATION, {}))
    assert res.block_reason is None


def test_command_chinese_stderr():
    """中文 stderr 不能乱码：Windows 上 exit 2 的原因按 OEM 代码页解码。"""
    hook = _hook(
        event=HookEvent.PRE_TOOL_USE,
        action=Action("command", command="echo 危险命令 1>&2 && exit 2"),
        timeout=5.0,
    )
    engine = HookEngine([hook])
    res = _run(engine.dispatch(HookEvent.PRE_TOOL_USE, {}))
    assert res.block_reason is not None
    assert "危险命令" in res.block_reason


def test_no_condition_always():
    calls = []

    async def runner(hook, payload):
        calls.append(1)
        return HookResult()

    engine = HookEngine([_hook(condition=None)], runner=runner)
    _run(engine.dispatch(HookEvent.NOTIFICATION, {"anything": "x"}))
    assert calls == [1]


def test_placeholder_subst():
    from helpcode.hook.actions import subst

    text = subst("{{tool}}|{{input.path}}", {"tool": "bash", "input": {"path": "a.py"}})
    assert text == "bash|a.py"
