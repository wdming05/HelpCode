"""Agent 多轮循环单测（假 Provider，新 Request 签名）。"""

import asyncio
import threading
import time

from helpcode.agent import Agent, Mode
from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent, TokenUsage, ToolCall
from helpcode.prompt import plan_reminder
from helpcode.tool import Registry, ToolResult


class FakeTool:
    """假工具：可配只读与否、执行延迟、共享结果记录。"""

    def __init__(self, name="fake_tool", read_only=True, delay=0.0, log=None):
        self.name = name
        self.read_only = read_only
        self.delay = delay
        self.log = log
        self.description = "假工具"
        self.parameters = {"type": "object", "properties": {}}

    def run(self, **kwargs):
        if self.delay:
            time.sleep(self.delay)
        if self.log is not None:
            self.log.append(self.name)
        return ToolResult(ok=True, content=f"{self.name} result")


class ConcurrencyTracker:
    """用 threading.Lock + 原子计数器记录并发峰值与完成顺序。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.completed = []


class TrackedTool:
    """带并发追踪的假工具。"""

    def __init__(self, name, read_only, delay, tracker):
        self.name = name
        self.read_only = read_only
        self.delay = delay
        self.tracker = tracker
        self.description = "d"
        self.parameters = {"type": "object", "properties": {}}

    def run(self, **kwargs):
        with self.tracker._lock:
            self.tracker.active += 1
            self.tracker.max_active = max(self.tracker.max_active, self.tracker.active)
        time.sleep(self.delay)
        with self.tracker._lock:
            self.tracker.active -= 1
            self.tracker.completed.append(self.name)
        return ToolResult(ok=True, content=self.name)


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, rounds):
        self.rounds = rounds  # list[list[StreamEvent]]
        self.call_count = 0
        self.requests = []  # 记录每次 stream 收到的 Request

    async def stream(self, req):
        self.requests.append(req)
        events = self.rounds[self.call_count]
        self.call_count += 1
        for ev in events:
            yield ev


def _collect(agent, text, conv, cancel, mode=Mode.NORMAL):
    async def _run():
        return [ev async for ev in agent.run(text, conv, cancel, mode)]

    return asyncio.run(_run())


def _tool_calls(ids):
    return [ToolCall(id=cid, name="fake_tool", input={}) for cid in ids]


# A：多轮循环
def test_agent_multi_round():
    rounds = [
        [StreamEvent(done=True, tool_calls=_tool_calls(["c1"]))],
        [StreamEvent(done=True, tool_calls=_tool_calls(["c2"]))],
        [StreamEvent(text="完成"), StreamEvent(done=True)],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool())
    agent = Agent(provider, registry, "dev")
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())

    iterations = [e.iteration for e in events if e.iteration is not None]
    assert iterations == [1, 2, 3]
    assert len([e for e in events if e.tool_start is not None]) == 2
    assert events[-1].done
    assert conv.messages()[-1].role == "assistant"
    assert conv.messages()[-1].content == "完成"


# B：纯文本一轮停
def test_agent_plain_text():
    rounds = [[StreamEvent(text="你好"), StreamEvent(done=True)]]
    provider = FakeProvider(rounds)
    agent = Agent(provider, Registry(), "dev")
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    assert all(e.tool_start is None for e in events)
    assert events[-1].done


# C：混合响应丢弃工具
def test_agent_mixed_drops_tools():
    rounds = [
        [
            StreamEvent(text="我来回答"),
            StreamEvent(done=True, tool_calls=_tool_calls(["c1"])),
        ]
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool())
    agent = Agent(provider, registry, "dev")
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    assert all(e.tool_end is None for e in events)
    assert conv.messages()[-1].content == "我来回答"


# D：并发只读 + 串行副作用
def test_agent_concurrent_readonly_serial_sideeffect():
    tracker = ConcurrencyTracker()
    registry = Registry()
    registry.register(TrackedTool("ro_a", read_only=True, delay=0.1, tracker=tracker))
    registry.register(TrackedTool("ro_b", read_only=True, delay=0.1, tracker=tracker))
    registry.register(
        TrackedTool("side_c", read_only=False, delay=0.02, tracker=tracker)
    )

    calls = [
        ToolCall(id="1", name="ro_a", input={}),
        ToolCall(id="2", name="ro_b", input={}),
        ToolCall(id="3", name="side_c", input={}),
    ]
    rounds = [
        [StreamEvent(done=True, tool_calls=calls)],
        [StreamEvent(text="完成"), StreamEvent(done=True)],
    ]
    agent = Agent(FakeProvider(rounds), registry, "dev")
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())

    assert tracker.max_active >= 2, f"并发峰值应 >=2，实际 {tracker.max_active}"
    assert tracker.completed[-1] == "side_c"
    tool_ends = [e for e in events if e.tool_end is not None]
    assert [e.tool_call.name for e in tool_ends] == ["ro_a", "ro_b", "side_c"]


# E：取消（历史合法）
def test_agent_cancel():
    rounds = [
        [StreamEvent(done=True, tool_calls=_tool_calls(["c1"]))],
        [StreamEvent(done=True, tool_calls=_tool_calls(["c2"]))],
        [StreamEvent(done=True, tool_calls=_tool_calls(["c3"]))],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool())
    agent = Agent(provider, registry, "dev")
    conv = Conversation()

    async def _run():
        cancel = asyncio.Event()
        events = []
        async for ev in agent.run("hi", conv, cancel):
            events.append(ev)
            if ev.tool_end is not None:
                cancel.set()
        return events

    events = asyncio.run(_run())
    assert any(e.notice == "已取消" for e in events)
    msgs = conv.messages()
    assert msgs[-1].role == "assistant"
    for m in msgs:
        if m.role == "assistant" and m.tool_calls:
            ids = {tc.id for tc in m.tool_calls}
            results = {mm.tool_call_id for mm in msgs if mm.role == "tool"}
            assert ids <= results, "存在未配对的工具调用"


# F：Plan Mode（tools 仅只读 + stable/environment/reminder）
def test_agent_plan_mode():
    rounds = [[StreamEvent(done=True)]]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool("ro", read_only=True))
    registry.register(FakeTool("w", read_only=False))
    agent = Agent(provider, registry, "dev")
    conv = Conversation()
    _collect(agent, "hi", conv, asyncio.Event(), mode=Mode.PLAN)
    req = provider.requests[0]
    assert [t["name"] for t in req.tools] == ["ro"]
    assert req.system.stable
    assert req.system.environment
    assert req.reminder == plan_reminder(True)


# G：迭代上限
def test_agent_max_iterations():
    rounds = [
        [StreamEvent(done=True, tool_calls=_tool_calls([f"c{i}"]))] for i in range(30)
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool())
    agent = Agent(provider, registry, "dev")
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    assert any(e.notice == "已达到最大迭代次数" for e in events)
    iterations = [e.iteration for e in events if e.iteration is not None]
    assert max(iterations) == 25


# H：连续未知工具
def test_agent_unknown_tool():
    rounds = [
        [
            StreamEvent(
                done=True,
                tool_calls=[ToolCall(id=f"c{i}", name="不存在", input={})],
            )
        ]
        for i in range(5)
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool())
    agent = Agent(provider, registry, "dev")
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    assert any(e.notice == "连续请求未知工具，已停止" for e in events)


# I：流出错
def test_agent_stream_error():
    rounds = [[StreamEvent(err=RuntimeError("boom"))]]
    provider = FakeProvider(rounds)
    agent = Agent(provider, Registry(), "dev")
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    assert any(e.err is not None for e in events)


# 新增：规划模式按轮次 reminder（iter1 完整、iter2 精简）
def test_plan_reminder_per_iteration():
    rounds = [
        [StreamEvent(done=True, tool_calls=_tool_calls(["c1"]))],
        [StreamEvent(text="完成"), StreamEvent(done=True)],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool())
    agent = Agent(provider, registry, "dev")
    conv = Conversation()
    _collect(agent, "hi", conv, asyncio.Event(), mode=Mode.PLAN)
    assert provider.requests[0].reminder == plan_reminder(True)
    assert provider.requests[1].reminder == plan_reminder(False)


# 新增：稳定系统提示跨模式一致
def test_stable_consistent_across_modes():
    rounds = [[StreamEvent(done=True)]]
    p1 = FakeProvider(rounds)
    reg1 = Registry()
    reg1.register(FakeTool())
    _collect(Agent(p1, reg1, "dev"), "hi", Conversation(), asyncio.Event())
    p2 = FakeProvider(rounds)
    reg2 = Registry()
    reg2.register(FakeTool())
    _collect(
        Agent(p2, reg2, "dev"), "hi", Conversation(), asyncio.Event(), mode=Mode.PLAN
    )
    assert p1.requests[0].system.stable == p2.requests[0].system.stable


# 新增：cache 用量透传
def test_cache_usage_transparent():
    rounds = [
        [
            StreamEvent(
                usage=TokenUsage(
                    input_tokens=10, output_tokens=5, cache_write=10, cache_read=8
                )
            ),
            StreamEvent(done=True),
        ]
    ]
    provider = FakeProvider(rounds)
    agent = Agent(provider, Registry(), "dev")
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    usages = [e.token_usage for e in events if e.token_usage is not None]
    assert usages and usages[0].cache_write == 10 and usages[0].cache_read == 8


# 新增：reminder 不写历史
def test_reminder_not_in_history():
    rounds = [
        [StreamEvent(done=True, tool_calls=_tool_calls(["c1"]))],
        [StreamEvent(text="完成"), StreamEvent(done=True)],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool())
    agent = Agent(provider, registry, "dev")
    conv = Conversation()
    _collect(agent, "hi", conv, asyncio.Event(), mode=Mode.PLAN)
    for m in conv.messages():
        assert "<system-reminder>" not in m.content
