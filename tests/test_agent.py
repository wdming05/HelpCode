"""Agent 多轮循环单测（假 Provider，权限集成）。"""

import asyncio
import tempfile
import threading
import time

from null_context import NullContext

from helpcode.agent import Agent
from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent, TokenUsage, ToolCall
from helpcode.permission import Mode, Outcome, new_engine
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


class _NullContext(NullContext):
    """永不压缩的桩；真正的压缩路径在 test_context_*.py 里。"""


def _make_agent(provider, registry):
    """构造 Agent（五参显式 + 临时目录引擎）。"""
    engine, _ = new_engine(tempfile.mkdtemp())
    return Agent(provider, registry, "dev", engine, _NullContext())


def _collect(agent, text, conv, cancel, mode=Mode.DEFAULT):
    async def _run():
        return [ev async for ev in agent.run(text, conv, cancel, mode)]

    return asyncio.run(_run())


def _tool_calls(ids, name="fake_tool"):
    return [ToolCall(id=cid, name=name, input={}) for cid in ids]


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
    agent = _make_agent(provider, registry)
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
    agent = _make_agent(provider, Registry())
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    assert all(e.tool_start is None for e in events)
    assert events[-1].done


# C：混合响应执行工具（text 引导语不参与是否结束的判断）
def test_agent_mixed_runs_tools():
    rounds = [
        [
            StreamEvent(text="我来回答"),
            StreamEvent(done=True, tool_calls=_tool_calls(["c1"])),
        ],
        [StreamEvent(text="工具已执行"), StreamEvent(done=True)],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool())
    agent = _make_agent(provider, registry)
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())

    # 工具被执行了（tool_start / tool_end 都有），而不是被引导语丢弃
    assert any(e.tool_start is not None for e in events)
    assert any(e.tool_end is not None for e in events)
    # 历史里记的是工具调用（assistant 带 tool_calls），不是引导语文本
    assert any(m.tool_calls for m in conv.messages())
    assert events[-1].done


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
    agent = _make_agent(FakeProvider(rounds), registry)
    conv = Conversation()
    # bypass 模式：非内置副作用工具归 EXEC 类会 Ask，bypass 全放行以隔离调度逻辑验证
    events = _collect(agent, "hi", conv, asyncio.Event(), mode=Mode.BYPASS)

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
    agent = _make_agent(provider, registry)
    conv = Conversation()

    async def _run():
        cancel = asyncio.Event()
        events = []
        async for ev in agent.run("hi", conv, cancel, Mode.DEFAULT):
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
    agent = _make_agent(provider, registry)
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
    agent = _make_agent(provider, registry)
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
    agent = _make_agent(provider, registry)
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    assert any(e.notice == "连续请求未知工具，已停止" for e in events)


# I：流出错
def test_agent_stream_error():
    rounds = [[StreamEvent(err=RuntimeError("boom"))]]
    provider = FakeProvider(rounds)
    agent = _make_agent(provider, Registry())
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    assert any(e.err is not None for e in events)


# 规划模式按轮次 reminder（iter1 完整、iter2 精简）
def test_plan_reminder_per_iteration():
    rounds = [
        [StreamEvent(done=True, tool_calls=_tool_calls(["c1"]))],
        [StreamEvent(text="完成"), StreamEvent(done=True)],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool())
    agent = _make_agent(provider, registry)
    conv = Conversation()
    _collect(agent, "hi", conv, asyncio.Event(), mode=Mode.PLAN)
    assert provider.requests[0].reminder == plan_reminder(True)
    assert provider.requests[1].reminder == plan_reminder(False)


# 稳定系统提示跨模式一致
def test_stable_consistent_across_modes():
    rounds = [[StreamEvent(done=True)]]
    p1 = FakeProvider(rounds)
    reg1 = Registry()
    reg1.register(FakeTool())
    _collect(_make_agent(p1, reg1), "hi", Conversation(), asyncio.Event())
    p2 = FakeProvider(rounds)
    reg2 = Registry()
    reg2.register(FakeTool())
    _collect(
        _make_agent(p2, reg2), "hi", Conversation(), asyncio.Event(), mode=Mode.PLAN
    )
    assert p1.requests[0].system.stable == p2.requests[0].system.stable


# cache 用量透传
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
    agent = _make_agent(provider, Registry())
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    usages = [e.token_usage for e in events if e.token_usage is not None]
    assert usages and usages[0].cache_write == 10 and usages[0].cache_read == 8


# reminder 不写历史
def test_reminder_not_in_history():
    rounds = [
        [StreamEvent(done=True, tool_calls=_tool_calls(["c1"]))],
        [StreamEvent(text="完成"), StreamEvent(done=True)],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool())
    agent = _make_agent(provider, registry)
    conv = Conversation()
    _collect(agent, "hi", conv, asyncio.Event(), mode=Mode.PLAN)
    for m in conv.messages():
        assert "<system-reminder>" not in m.content


# ============ ch06 权限集成 ============


# 黑名单拒绝回灌（不中断，Loop 继续）
def test_agent_deny_blacklist():
    rounds = [
        [
            StreamEvent(
                done=True,
                tool_calls=[
                    ToolCall(id="c1", name="bash", input={"command": "rm -rf /"})
                ],
            )
        ],
        [StreamEvent(text="我改用别的方式"), StreamEvent(done=True)],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool("bash", read_only=False))
    agent = _make_agent(provider, registry)
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())

    deny_ends = [e.tool_end for e in events if e.tool_end is not None]
    assert any("黑名单" in r.content and r.is_error for r in deny_ends), (
        "应回灌黑名单拒绝"
    )
    assert conv.messages()[-1].content == "我改用别的方式", "Loop 应继续"


# 人在回路：Ask → 允许本次
def test_agent_ask_allow_once():
    rounds = [
        [
            StreamEvent(
                done=True,
                tool_calls=[
                    ToolCall(id="c1", name="write_file", input={"path": "x.txt"})
                ],
            )
        ],
        [StreamEvent(text="写完了"), StreamEvent(done=True)],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool("write_file", read_only=False))
    agent = _make_agent(provider, registry)
    conv = Conversation()

    async def _run():
        events = []
        async for ev in agent.run("写", conv, asyncio.Event(), Mode.DEFAULT):
            if ev.permission_request is not None:
                ev.permission_request.respond.set_result(Outcome.ALLOW_ONCE)
            events.append(ev)
        return events

    events = asyncio.run(_run())
    assert any(e.permission_request is not None for e in events), "应触发 Ask"
    assert any(e.tool_end is not None and e.tool_end.ok for e in events), "允许后应执行"


# 人在回路：拒绝本次
def test_agent_ask_deny_once():
    rounds = [
        [
            StreamEvent(
                done=True,
                tool_calls=[
                    ToolCall(id="c1", name="write_file", input={"path": "x.txt"})
                ],
            )
        ],
        [StreamEvent(text="那我换个方案"), StreamEvent(done=True)],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool("write_file", read_only=False))
    agent = _make_agent(provider, registry)
    conv = Conversation()

    async def _run():
        async for ev in agent.run("写", conv, asyncio.Event(), Mode.DEFAULT):
            if ev.permission_request is not None:
                ev.permission_request.respond.set_result(Outcome.DENY_ONCE)

    asyncio.run(_run())
    msgs = conv.messages()
    # 拒绝后工具结果应含"拒绝"，且 Loop 继续
    tool_results = [m for m in msgs if m.role == "tool"]
    assert any("拒绝" in r.content for m in tool_results for r in m.tool_results)


# 未知工具（registry 不存在）→ 报错不弹窗
def test_agent_unknown_tool_no_ask():
    rounds = [
        [
            StreamEvent(
                done=True,
                tool_calls=[ToolCall(id="c1", name="ghost_tool", input={})],
            )
        ],
        [StreamEvent(text="x"), StreamEvent(done=True)],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool())
    agent = _make_agent(provider, registry)
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    assert not any(e.permission_request is not None for e in events), "未知工具不应弹窗"
    assert any(
        e.tool_end is not None and "未知工具" in e.tool_end.content for e in events
    )


# 只读批不产生 approval 事件（并发不退化）
def test_agent_readonly_no_approval():
    rounds = [
        [
            StreamEvent(
                done=True,
                tool_calls=[
                    ToolCall(id="1", name="read_file", input={"path": "a.txt"}),
                    ToolCall(id="2", name="read_file", input={"path": "b.txt"}),
                ],
            )
        ],
        [StreamEvent(text="读完"), StreamEvent(done=True)],
    ]
    provider = FakeProvider(rounds)
    registry = Registry()
    registry.register(FakeTool("read_file", read_only=True))
    agent = _make_agent(provider, registry)
    conv = Conversation()
    events = _collect(agent, "hi", conv, asyncio.Event())
    assert not any(e.permission_request is not None for e in events), "只读不应触发 Ask"


# ---------------------------------------------------------------- ch08：压缩与撞墙

from helpcode.context import CompressInfo, CompressReport
from helpcode.llm import ContextLengthExceeded

PTL = ContextLengthExceeded("prompt is too long")


class _RetryContext(_NullContext):
    """可配置的压缩桩：用来验证 agent 侧的撞墙重试分支。"""

    def __init__(self, after=1000, failed="", compress=False, with_info=False):
        self.compact_calls = 0
        self.before_calls = 0
        self._after = after
        self._failed = failed
        self._compress = compress
        self._with_info = with_info

    def estimate(self, conv):
        return 50_000

    def should_compress(self, conv):
        return self._compress

    async def before_request(self, conv, cancel_event, tool_names):
        self.before_calls += 1
        if not self._with_info:
            return CompressReport()
        return CompressReport(
            info=CompressInfo(
                phase="done",
                tokens_before=50_000,
                tokens_after=1_000,
                summarized=4,
                kept=3,
            )
        )

    async def compact_now(self, conv, emergency=False, tool_names=None):
        self.compact_calls += 1
        return CompressReport(
            tokens_after=self._after,
            failed=self._failed,
            summarized=2,
            kept=3,
            notice="已压缩：摘要 2 条 / 保留 3 条",
        )

    def has_room_after_compress(self, tokens_after):
        return tokens_after < 100_000


def _make_agent_ctx(provider, registry, ctx):
    engine, _ = new_engine(tempfile.mkdtemp())
    return Agent(provider, registry, "dev", engine, ctx)


def _text_round(text="ok"):
    return [
        StreamEvent(text=text),
        StreamEvent(usage=TokenUsage(input_tokens=10, output_tokens=2)),
        StreamEvent(done=True),
    ]


def test_compress_events_before_request():
    provider = FakeProvider([_text_round()])
    ctx = _RetryContext(compress=True, with_info=True)
    agent = _make_agent_ctx(provider, Registry(), ctx)

    events = _collect(agent, "hi", Conversation(), asyncio.Event())
    compress = [e.compress for e in events if e.compress]

    assert [c.phase for c in compress] == ["start", "done"]
    assert compress[0].tokens_before == 50_000
    assert compress[1].tokens_after == 1_000
    # start 在任何正文之前
    first_text = next(i for i, e in enumerate(events) if e.text)
    first_compress = next(i for i, e in enumerate(events) if e.compress)
    assert first_compress < first_text


def test_wall_hit_triggers_emergency_then_retry():
    provider = FakeProvider([[StreamEvent(err=PTL)], _text_round("救回来了")])
    ctx = _RetryContext()
    agent = _make_agent_ctx(provider, Registry(), ctx)

    events = _collect(agent, "hi", Conversation(), asyncio.Event())

    assert provider.call_count == 2
    assert ctx.compact_calls == 1
    assert any(e.text == "救回来了" for e in events)
    phases = [e.compress.phase for e in events if e.compress]
    assert phases == ["start", "done"]


def test_wall_hit_retries_only_once():
    provider = FakeProvider(
        [[StreamEvent(err=PTL)], [StreamEvent(err=PTL)], _text_round()]
    )
    ctx = _RetryContext()
    agent = _make_agent_ctx(provider, Registry(), ctx)

    events = _collect(agent, "hi", Conversation(), asyncio.Event())

    assert provider.call_count == 2  # 第二次仍失败 → 不再重试
    assert any(e.err is PTL for e in events)


def test_wall_hit_after_text_does_not_retry():
    """已经流出文本再撞墙：重试会让同一段话出现两次，必须直接上报。"""
    provider = FakeProvider([[StreamEvent(text="半句"), StreamEvent(err=PTL)]])
    ctx = _RetryContext()
    agent = _make_agent_ctx(provider, Registry(), ctx)

    events = _collect(agent, "hi", Conversation(), asyncio.Event())

    assert provider.call_count == 1
    assert ctx.compact_calls == 0
    assert any(e.err is PTL for e in events)


def test_wall_hit_when_compact_cannot_help_reports_error():
    provider = FakeProvider([[StreamEvent(err=PTL)]])
    ctx = _RetryContext(after=200_000)  # 压完仍贴窗口
    agent = _make_agent_ctx(provider, Registry(), ctx)

    events = _collect(agent, "hi", Conversation(), asyncio.Event())

    assert provider.call_count == 1
    assert any(e.err is PTL for e in events)


def test_wall_hit_when_compact_failed_reports_error():
    provider = FakeProvider([[StreamEvent(err=PTL)]])
    ctx = _RetryContext(failed="摘要输出不可解析")
    agent = _make_agent_ctx(provider, Registry(), ctx)

    events = _collect(agent, "hi", Conversation(), asyncio.Event())

    assert provider.call_count == 1
    assert any(e.err is PTL for e in events)


def test_non_ptl_error_not_retried():
    boom = ValueError("rate limit")
    provider = FakeProvider([[StreamEvent(err=boom)]])
    ctx = _RetryContext()
    agent = _make_agent_ctx(provider, Registry(), ctx)

    events = _collect(agent, "hi", Conversation(), asyncio.Event())

    assert provider.call_count == 1
    assert ctx.compact_calls == 0
    assert any(e.err is boom for e in events)


def test_compact_now_delegates():
    ctx = _RetryContext()
    agent = _make_agent_ctx(FakeProvider([]), Registry(), ctx)

    report = asyncio.run(agent.compact_now(Conversation()))

    assert ctx.compact_calls == 1
    assert "已压缩" in report.notice


def test_no_compress_event_when_below_threshold():
    """未触发阈值时事件流里不该有 compress 事件（TUI 不该闪过"正在压缩"）。"""
    provider = FakeProvider([_text_round()])
    ctx = _RetryContext(compress=False)
    agent = _make_agent_ctx(provider, Registry(), ctx)

    events = _collect(agent, "hi", Conversation(), asyncio.Event())

    assert not [e for e in events if e.compress]
    assert ctx.before_calls == 1


def test_agent_requires_context_argument():
    """不传 context 必须当场 TypeError，而不是跑到一半才发现没压缩。"""
    engine, _ = new_engine(tempfile.mkdtemp())
    try:
        Agent(FakeProvider([]), Registry(), "dev", engine)  # type: ignore[call-arg]
    except TypeError as e:
        assert "context" in str(e)
    else:  # pragma: no cover
        raise AssertionError("缺少 context 参数应抛 TypeError")


# ---------------------------------------------------------------- ch09：快照与笔记


def _agent_with_notes(provider, on_turn_end=None, extra=("", "")):
    engine, _ = new_engine(tempfile.mkdtemp())
    return Agent(
        provider,
        Registry(),
        "dev",
        engine,
        NullContext(),
        system_extra=extra,
        on_turn_end=on_turn_end,
    )


def test_system_extra_used():
    """两段文本进了 system.stable（假 provider 收到的请求里能查到）。"""
    provider = FakeProvider([_text_round()])
    agent = _agent_with_notes(provider, extra=("项目规则X", "记忆索引Y"))

    _collect(agent, "hi", Conversation(), asyncio.Event())

    stable = provider.requests[0].system.stable
    assert "项目规则X" in stable
    assert "记忆索引Y" in stable
    assert stable.index("项目规则X") < stable.index("记忆索引Y")


def test_system_stable_same_across_turns():
    """同一会话两轮：system.stable 逐字节相同（AC29）。

    这条是「快照」的全部意义：如果每轮重读索引，笔记一写文件就会让 stable
    变形，ch05 的 prompt 缓存整段失效——而且用户完全看不出来。
    """
    provider = FakeProvider([_text_round("一"), _text_round("二")])
    agent = _agent_with_notes(provider, extra=("规则", "索引"))
    conv = Conversation()

    _collect(agent, "q1", conv, asyncio.Event())
    _collect(agent, "q2", conv, asyncio.Event())

    assert provider.requests[0].system.stable == provider.requests[1].system.stable


def test_on_turn_end_called_on_natural_stop():
    """自然停下（无工具调用）→ 触发一次笔记任务。"""
    called = []

    async def hook(conv):
        called.append(conv)

    provider = FakeProvider([_text_round()])
    agent = _agent_with_notes(provider, on_turn_end=hook)

    _collect(agent, "hi", Conversation(), asyncio.Event())
    asyncio.run(agent.drain_note_tasks())

    assert len(called) == 1


def test_on_turn_end_not_called_on_tool_turn():
    """工具轮不触发：那一轮还没有「结论」，提炼出来的是半截推理。"""
    called = []

    async def hook(conv):
        called.append(conv)

    provider = FakeProvider(
        [[StreamEvent(done=True, tool_calls=_tool_calls(["c1"]))], _text_round()]
    )
    registry = Registry()
    registry.register(FakeTool())
    engine, _ = new_engine(tempfile.mkdtemp())
    agent = Agent(provider, registry, "dev", engine, NullContext(), on_turn_end=hook)

    _collect(agent, "hi", Conversation(), asyncio.Event())
    asyncio.run(agent.drain_note_tasks())

    assert len(called) == 1  # 只有最后那轮自然停下触发


def test_memory_signal_bypasses_interval(monkeypatch):
    """间隔设为 5 时，第 2 轮说「记住」仍立即触发。"""
    import helpcode.agent as agent_mod

    called = []

    async def hook(conv):
        called.append(conv)

    monkeypatch.setattr(agent_mod, "MEMORY_UPDATE_EVERY", 5)
    provider = FakeProvider([_text_round("一"), _text_round("二")])
    agent = _agent_with_notes(provider, on_turn_end=hook)
    conv = Conversation()

    _collect(agent, "第一个问题", conv, asyncio.Event())  # 第 1 轮：1 % 5 != 0
    _collect(agent, "记住：以后都跑 ruff", conv, asyncio.Event())
    asyncio.run(agent.drain_note_tasks())

    assert len(called) == 1


def test_note_request_usage_not_counted():
    """笔记请求不产生第二个 usage 事件：主历史只认自己那一轮的用量（AC24）。

    笔记走的是独立 provider 调用。若把它也算进 `token_usage`，状态栏的
    上下文占用会莫名其妙地多出一截，用户会以为历史变长了。
    """
    seen: list[list] = []

    async def hook(conv):
        # 模拟笔记请求自己烧了一遍 token —— 它不该出现在主历史的 usage 里
        seen.append([m.content for m in conv.messages()])

    provider = FakeProvider([_text_round()])
    agent = _agent_with_notes(provider, on_turn_end=hook)

    events = _collect(agent, "hi", Conversation(), asyncio.Event())
    asyncio.run(agent.drain_note_tasks())

    usages = [e.token_usage for e in events if e.token_usage is not None]
    assert len(usages) == 1  # 只有主请求那一次
    assert usages[0].input_tokens == 10 and usages[0].output_tokens == 2
    assert len(seen) == 1  # 笔记任务确实跑了


def test_on_turn_end_not_called_on_error_round():
    """报错轮不触发笔记任务：那一轮没有结论，提炼出来只会是半截推理。

    取消轮同理——`CancelledError` 会一路冒泡出 `run()`，走不到写历史的
    那个分支。这里锁住报错那条。
    """
    called: list = []

    async def hook(conv):
        called.append(conv)

    provider = FakeProvider([[StreamEvent(err=RuntimeError("boom"))]])
    agent = _agent_with_notes(provider, on_turn_end=hook)

    events = _collect(agent, "hi", Conversation(), asyncio.Event())
    asyncio.run(agent.drain_note_tasks())

    assert any(e.err is not None for e in events)
    assert called == []
