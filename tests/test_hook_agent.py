"""Agent 接入 Hook：PreToolUse 拦截 / PostToolUse / PreUserMessage / Stop。"""

import asyncio
import tempfile

from null_context import NullContext

from helpcode.agent import Agent
from helpcode.conversation import Conversation
from helpcode.hook import HookEngine, HookEvent
from helpcode.hook.schema import Action, Hook, HookResult
from helpcode.llm import StreamEvent, ToolCall
from helpcode.permission import Mode, new_engine
from helpcode.tool import Registry, ToolResult


class FakeTool:
    def __init__(self, name="bash", read_only=False):
        self.name = name
        self.read_only = read_only
        self.description = "fake"
        self.parameters = {"type": "object", "properties": {}}
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        return ToolResult(ok=True, content="ran")


class FakeProvider:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.requests = []

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "m"

    async def stream(self, req):
        self.requests.append(req)
        for ev in self.rounds.pop(0):
            yield ev


def _collect(agent, text="hi"):
    async def _go():
        return [
            ev
            # BYPASS：跳过权限 ASK，避免测试里无人应答导致挂起
            async for ev in agent.run(
                text, Conversation(), asyncio.Event(), Mode.BYPASS
            )
        ]

    return asyncio.run(_go())


def _hook(name, event, action=None, condition=None, only_once=False):
    return Hook(
        name=name,
        event=event,
        condition=condition,
        action=action or Action("prompt", prompt="p"),
        only_once=only_once,
    )


def _agent(provider, registry, hooks):
    engine, _ = new_engine(tempfile.mkdtemp())
    return Agent(provider, registry, "dev", engine, NullContext(), hooks=hooks)


def _tool_round(name="bash"):
    return [StreamEvent(done=True, tool_calls=[ToolCall(id="c1", name=name, input={})])]


def test_pre_tool_blocks():
    async def runner(hook, payload):
        if hook.event is HookEvent.PRE_TOOL_USE:
            return HookResult(block_reason="危险命令被禁止")
        return HookResult()

    provider = FakeProvider(
        [_tool_round(), [StreamEvent(text="好的"), StreamEvent(done=True)]]
    )
    registry = Registry()
    tool = FakeTool()
    registry.register(tool)
    hooks = HookEngine([_hook("b", HookEvent.PRE_TOOL_USE)], runner=runner)
    agent = _agent(provider, registry, hooks)

    events = _collect(agent)
    assert tool.calls == 0  # 被拦，未执行
    ends = [e.tool_end for e in events if e.tool_end is not None]
    assert any("危险命令被禁止" in r.content and r.is_error for r in ends)


def test_pre_tool_allows():
    async def runner(hook, payload):
        return HookResult()

    provider = FakeProvider(
        [_tool_round(), [StreamEvent(text="完成"), StreamEvent(done=True)]]
    )
    registry = Registry()
    tool = FakeTool()
    registry.register(tool)
    hooks = HookEngine([_hook("a", HookEvent.PRE_TOOL_USE)], runner=runner)
    agent = _agent(provider, registry, hooks)

    _collect(agent)
    assert tool.calls == 1


def test_post_tool_fires():
    seen = []

    async def runner(hook, payload):
        if hook.event is HookEvent.POST_TOOL_USE:
            seen.append(payload)
        return HookResult()

    provider = FakeProvider(
        [_tool_round(), [StreamEvent(text="完成"), StreamEvent(done=True)]]
    )
    registry = Registry()
    registry.register(FakeTool())
    hooks = HookEngine([_hook("p", HookEvent.POST_TOOL_USE)], runner=runner)
    agent = _agent(provider, registry, hooks)

    _collect(agent)
    assert len(seen) == 1
    assert seen[0]["tool"] == "bash"
    assert seen[0]["is_error"] is False


def test_post_tool_error_flag():
    seen = []

    async def runner(hook, payload):
        seen.append(payload)
        return HookResult()

    provider = FakeProvider(
        [_tool_round(), [StreamEvent(text="完成"), StreamEvent(done=True)]]
    )
    registry = Registry()
    registry.register(FakeTool(read_only=True))
    hooks = HookEngine([_hook("p", HookEvent.POST_TOOL_USE)], runner=runner)
    agent = _agent(provider, registry, hooks)
    # 把工具换成会报错的
    registry._tools["bash"].run = lambda **kw: ToolResult(
        ok=False, content="x", is_error=True
    )

    _collect(agent)
    assert seen and seen[0]["is_error"] is True


def test_pre_user_message_inject():
    async def runner(hook, payload):
        return HookResult(injected=["请用中文"])

    provider = FakeProvider([[StreamEvent(text="ok"), StreamEvent(done=True)]])
    hooks = HookEngine([_hook("u", HookEvent.PRE_USER_MESSAGE)], runner=runner)
    agent = _agent(provider, Registry(), hooks)

    _collect(agent)
    assert "请用中文" in provider.requests[0].reminder


def test_stop_fires():
    fired = []

    async def runner(hook, payload):
        if hook.event is HookEvent.STOP:
            fired.append(payload)
        return HookResult()

    provider = FakeProvider([[StreamEvent(text="ok"), StreamEvent(done=True)]])
    hooks = HookEngine([_hook("s", HookEvent.STOP)], runner=runner)
    agent = _agent(provider, Registry(), hooks)

    _collect(agent)
    assert len(fired) == 1


def test_no_hooks_unchanged():
    provider = FakeProvider([[StreamEvent(text="ok"), StreamEvent(done=True)]])
    agent = _agent(provider, Registry(), None)
    events = _collect(agent)
    assert any(e.text == "ok" for e in events)
    assert any(e.done for e in events)
