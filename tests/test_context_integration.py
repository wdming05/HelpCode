"""集成测试：真 Agent + 真 ContextManager + 假 provider。

单测各自绿不代表组合起来也对——历史和配对问题是「组合」出来的，
所以这里必须用真 Agent 跑完整 run()。
"""

import asyncio
import tempfile

import pytest
from fake_provider import FakeProvider

from helpcode.agent import Agent
from helpcode.context import ContextManager
from helpcode.context import summarize as summarize_mod
from helpcode.conversation import Conversation
from helpcode.llm import ContextLengthExceeded, ToolCall
from helpcode.permission import Mode, new_engine
from helpcode.tool import Registry, ToolResult

GOOD_SUMMARY = "<summary>" + "正式摘要内容。" * 20 + "</summary>"
PTL = ContextLengthExceeded("prompt is too long")


def _conv(rounds: int = 2, size: int = 400) -> Conversation:
    conv = Conversation()
    for i in range(rounds):
        conv.add_user(f"问题{i} " + "x" * size)
        conv.add_assistant_tool_calls(
            [ToolCall(id=f"c{i}", name="bash", input={"command": "ls"})]
        )
        conv.add_tool_result(f"c{i}", ToolResult(ok=True, content="y" * size))
        conv.add_assistant("好的")
    return conv


def _agent(provider, ctx):
    engine, _ = new_engine(tempfile.mkdtemp())
    return Agent(provider, Registry(), "dev", engine, ctx)


def _collect(agent, conv):
    async def _run():
        return [
            ev async for ev in agent.run("继续", conv, asyncio.Event(), Mode.DEFAULT)
        ]

    return asyncio.run(_run())


def assert_valid_history(msgs):
    assert msgs, "历史不能为空"
    assert msgs[0].role != "tool", "历史不能以 tool 开头"
    pending: set[str] = set()
    for m in msgs:
        if m.role == "assistant" and m.tool_calls:
            pending = {tc.id for tc in m.tool_calls}
        elif m.role == "tool":
            assert m.tool_call_id in pending, f"孤儿 tool: {m.tool_call_id}"
            pending.discard(m.tool_call_id)


@pytest.fixture
def tiny(monkeypatch):
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_TOKENS", 100)
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_MIN_MSGS", 2)


def test_wall_hit_end_to_end(tmp_path, tiny):
    """首次撞墙 → 紧急压缩 → 同迭代重试成功（provider 共 3 次调用）。"""
    provider = FakeProvider([PTL, GOOD_SUMMARY, "修好了"])
    ctx = ContextManager(provider, tmp_path, 128000)
    agent = _agent(provider, ctx)
    conv = _conv(2)

    events = _collect(agent, conv)

    assert provider.call_count == 3  # 主请求 + 摘要 + 重试
    assert any(e.text == "修好了" for e in events)
    assert not any(e.err for e in events)
    assert_valid_history(conv.messages())


def test_wall_hit_after_text_end_to_end(tmp_path, tiny):
    """已经吐了半句才撞墙：不重试，直接上报（避免重复输出）。"""
    provider = FakeProvider([["半句", PTL]])
    ctx = ContextManager(provider, tmp_path, 128000)
    agent = _agent(provider, ctx)

    events = _collect(agent, _conv(2))

    assert provider.call_count == 1
    assert any(e.text == "半句" for e in events)
    assert any(e.err is not None for e in events)


def test_compress_in_agent_keeps_history_valid(tmp_path, tiny, monkeypatch):
    """真 Agent 走完一轮压缩，历史仍然合法。"""
    from helpcode.context import budget as budget_mod

    monkeypatch.setattr(budget_mod, "SAFETY_AUTO", 100)
    provider = FakeProvider([GOOD_SUMMARY, "完成"])
    ctx = ContextManager(provider, tmp_path, 1000)
    agent = _agent(provider, ctx)
    conv = _conv(3, 800)  # 3 轮 × 800 字符 ≈ 1300 token，超过触发线 900

    events = _collect(agent, conv)

    assert_valid_history(conv.messages())
    assert any(e.compress and e.compress.phase == "done" for e in events)
    assert any(e.text == "完成" for e in events)
