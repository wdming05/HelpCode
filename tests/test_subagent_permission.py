"""子 Agent 权限：dontAsk / 审批回调 / 默认仍 Ask。"""

import asyncio

from null_context import NullContext

from helpcode.agent import Agent
from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent, ToolCall
from helpcode.permission import Mode, new_engine
from helpcode.tool import Registry, ToolResult


class _Provider:
    def __init__(self, rounds):
        self.rounds = list(rounds)

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "m"

    async def stream(self, req):
        for ev in self.rounds.pop(0):
            yield ev


class _Tool:
    def __init__(self):
        self.name = "bash"
        self.read_only = False
        self.description = "fake bash"
        self.parameters = {"type": "object", "properties": {}}
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        return ToolResult(ok=True, content="executed")


def _rounds():
    return [
        [StreamEvent(done=True, tool_calls=[ToolCall(id="c1", name="bash", input={})])],
        [StreamEvent(text="完成"), StreamEvent(done=True)],
    ]


def _agent(provider, tool, **kw):
    reg = Registry()
    reg.register(tool)
    engine, _ = new_engine(".")
    return Agent(provider, reg, "t", engine, NullContext(), **kw)


def _collect(agent, cancel=None):
    async def go():
        evs = [
            ev
            async for ev in agent.run(
                "hi", Conversation(), cancel or asyncio.Event(), Mode.DEFAULT
            )
        ]
        return evs

    return asyncio.run(go())


def test_dont_ask_allows():
    tool = _Tool()
    agent = _agent(_Provider(_rounds()), tool, auto_approve_ask=True)
    events = _collect(agent)
    assert tool.calls == 1
    assert not any(e.permission_request is not None for e in events)


def test_approval_handler():
    tool = _Tool()
    seen = []

    async def handler(call, reason):
        from helpcode.permission import Outcome

        seen.append(call.name)
        return Outcome.ALLOW_ONCE

    agent = _agent(_Provider(_rounds()), tool, approval_handler=handler)
    _collect(agent)
    assert seen == ["bash"]
    assert tool.calls == 1


def test_approval_handler_deny():
    tool = _Tool()
    from helpcode.permission import Outcome

    async def handler(call, reason):
        return Outcome.DENY_ONCE

    agent = _agent(_Provider(_rounds()), tool, approval_handler=handler)
    events = _collect(agent)
    assert tool.calls == 0
    assert any(e.tool_end is not None and "拒绝" in e.tool_end.content for e in events)


def test_default_still_asks():
    tool = _Tool()
    agent = _agent(_Provider(_rounds()), tool)
    cancel = asyncio.Event()

    async def go():
        seen_request = False
        async for ev in agent.run("hi", Conversation(), cancel, Mode.DEFAULT):
            if ev.permission_request is not None:
                seen_request = True
                cancel.set()  # 解除等待，避免挂起
        return seen_request

    assert asyncio.run(go()) is True
    assert tool.calls == 0
