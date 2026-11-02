"""压缩开启下的多轮压力：历史必须始终合法，且压缩真的发生了。

provider 是「智能桩」：摘要请求返回摘要，普通请求返回 ok，并报告一个
偏大的 input_tokens，让锚点把总量推到触发线以上。
"""

import asyncio
import tempfile

from helpcode.agent import Agent
from helpcode.context import ContextManager
from helpcode.context import budget as budget_mod
from helpcode.context import summarize as summarize_mod
from helpcode.context.summarize import SUMMARY_SYSTEM
from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent, TokenUsage, ToolCall
from helpcode.permission import Mode, new_engine
from helpcode.tool import Registry, ToolResult

GOOD_SUMMARY = "<summary>" + "正式摘要内容。" * 20 + "</summary>"


class SmartProvider:
    name = "smart"
    model = "smart"
    context_window = 128000

    def __init__(self, reported_tokens: int = 5000) -> None:
        self.calls = 0
        self.summary_calls = 0
        self._reported = reported_tokens

    async def stream(self, req):
        self.calls += 1
        if req.system.stable == SUMMARY_SYSTEM:
            self.summary_calls += 1
            yield StreamEvent(text=GOOD_SUMMARY)
        else:
            yield StreamEvent(text="ok")
        yield StreamEvent(
            usage=TokenUsage(input_tokens=self._reported, output_tokens=10)
        )
        yield StreamEvent(done=True)


def _conv(rounds: int = 3, size: int = 800) -> Conversation:
    conv = Conversation()
    for i in range(rounds):
        conv.add_user(f"问题{i} " + "x" * size)
        conv.add_assistant_tool_calls(
            [ToolCall(id=f"c{i}", name="bash", input={"command": "ls"})]
        )
        conv.add_tool_result(f"c{i}", ToolResult(ok=True, content="y" * size))
        conv.add_assistant("好的")
    return conv


def assert_valid_history(msgs):
    assert msgs
    assert msgs[0].role != "tool"
    pending: set[str] = set()
    for m in msgs:
        if m.role == "assistant" and m.tool_calls:
            pending = {tc.id for tc in m.tool_calls}
        elif m.role == "tool":
            assert m.tool_call_id in pending
            pending.discard(m.tool_call_id)


def _run_once(agent, conv):
    async def _go():
        return [
            ev async for ev in agent.run("继续", conv, asyncio.Event(), Mode.DEFAULT)
        ]

    return asyncio.run(_go())


def test_multi_round_compression_keeps_history_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(budget_mod, "SAFETY_AUTO", 100)
    # 保留区也压小，否则 10000 的目标比整段历史还大，find_cut 永远返回 None
    monkeypatch.setattr(budget_mod, "KEEP_TAIL_MIN_MSGS", 2)
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_TOKENS", 300)
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_MIN_MSGS", 2)
    provider = SmartProvider()
    ctx = ContextManager(provider, tmp_path, 1000)
    engine, _ = new_engine(tempfile.mkdtemp())
    agent = Agent(provider, Registry(), "dev", engine, ctx)
    conv = _conv()

    for _ in range(8):
        _run_once(agent, conv)
        assert_valid_history(conv.messages())

    assert provider.summary_calls >= 3, "8 轮里应至少压缩 3 次"
    # 压缩后历史以 user 摘要开头
    assert conv.messages()[0].role == "user"


def test_fifteen_rounds_stays_valid_and_bounded(tmp_path, monkeypatch):
    """15 轮长会话：历史始终合法，压缩多次发生后条数不再线性增长。"""
    import asyncio

    from fake_provider import FakeProvider

    from helpcode.context import budget as budget_mod
    from helpcode.context import summarize as summarize_mod
    from helpcode.context.budget import ContextManager
    from helpcode.conversation import Conversation
    from helpcode.llm import ToolCall
    from helpcode.tool import ToolResult

    monkeypatch.setattr(budget_mod, "SAFETY_AUTO", 100)
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_TOKENS", 100)
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_MIN_MSGS", 2)
    monkeypatch.setattr(summarize_mod, "MIN_SUMMARIZE_MSGS", 2)

    summary = "<summary>" + "摘要内容。" * 30 + "</summary>"
    provider = FakeProvider([summary] * 30)
    conv = Conversation()
    cm = ContextManager(provider, tmp_path, 1000)

    sizes = []
    for i in range(15):
        conv.add_user(f"轮{i} " + "x" * 400)
        conv.add_assistant_tool_calls(
            [ToolCall(id=f"c{i}", name="bash", input={"command": "ls"})]
        )
        conv.add_tool_result(f"c{i}", ToolResult(ok=True, content="y" * 400))
        conv.add_assistant("好")
        asyncio.run(cm.before_request(conv, None, ["bash"]))
        sizes.append(len(conv.messages()))

    msgs = conv.messages()
    assert msgs[0].role != "tool"
    pending: set[str] = set()
    for m in msgs:
        if m.role == "assistant" and m.tool_calls:
            pending = {tc.id for tc in m.tool_calls}
        elif m.role == "tool":
            assert m.tool_call_id in pending

    assert max(sizes) < 15 * 4  # 有压缩发生，没有线性堆到 60 条
    assert provider.call_count >= 3
