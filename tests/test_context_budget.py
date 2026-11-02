"""ContextManager 编排单测。"""

import asyncio
import re

import pytest
from fake_provider import FakeProvider

from helpcode.context import budget
from helpcode.context import summarize as summarize_mod
from helpcode.context.budget import ContextManager
from helpcode.conversation import Conversation
from helpcode.llm import TokenUsage, ToolCall
from helpcode.tool import ToolResult

GOOD_SUMMARY = "<summary>" + "正式摘要内容。" * 20 + "</summary>"


def _run(coro):
    return asyncio.run(coro)


def _conv(rounds: int = 6, size: int = 400) -> Conversation:
    """rounds 轮「user → assistant(tool_calls) → tool → assistant」。"""
    conv = Conversation()
    for i in range(rounds):
        conv.add_user(f"问题{i} " + "x" * size)
        conv.add_assistant_tool_calls(
            [ToolCall(id=f"c{i}", name="bash", input={"command": "ls"})]
        )
        conv.add_tool_result(f"c{i}", ToolResult(ok=True, content="y" * size))
        conv.add_assistant("好的")
    return conv


@pytest.fixture
def small(monkeypatch):
    """把阈值压小，让几十条消息就能触发压缩。"""
    monkeypatch.setattr(budget, "SAFETY_AUTO", 100)
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_TOKENS", 100)
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_MIN_MSGS", 2)


# ---------------------------------------------------------------- 估算与锚点


def test_anchor_uses_input_plus_cache_read_and_write(tmp_path):
    """★ 只取 input_tokens 会把上下文低估一个数量级，压缩永远不触发。"""
    cm = ContextManager(FakeProvider(), tmp_path, 1000)
    cm.after_stream(
        TokenUsage(input_tokens=100, output_tokens=10, cache_read=900, cache_write=0),
        0,
    )
    assert cm.estimate(Conversation()) == 1000


def test_anchor_adds_increment(tmp_path):
    conv = _conv(1, 40)  # 4 条消息
    cm = ContextManager(FakeProvider(), tmp_path, 10_000)
    cm.after_stream(TokenUsage(input_tokens=500), message_count=0)
    base = cm.estimate(conv)
    assert base > 500


def test_anchor_reset_falls_back_to_full(tmp_path, small):
    conv = _conv(2)
    cm = ContextManager(FakeProvider(), tmp_path, 1000)
    cm.after_stream(TokenUsage(input_tokens=50_000), message_count=0)
    cm.reset_anchor()
    assert cm.estimate(conv) < 50_000


# ---------------------------------------------------------------- 阈值


def test_should_compress_above_threshold(tmp_path, small):
    conv = _conv(6)
    cm = ContextManager(FakeProvider(), tmp_path, 1000)
    assert cm.should_compress(conv)


def test_should_not_compress_below_threshold(tmp_path, small):
    conv = Conversation()
    conv.add_user("hi")
    cm = ContextManager(FakeProvider(), tmp_path, 1000)
    assert not cm.should_compress(conv)


def test_window_below_threshold_disables_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "SAFETY_AUTO", 13000)  # 不受 FORCE_COMPRESS 影响
    cm = ContextManager(FakeProvider(), tmp_path, 5000)
    assert not cm.auto_enabled
    report = _run(cm.before_request(Conversation(), None, []))
    assert "过小" in report.notice


def test_auto_disabled_notice_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "SAFETY_AUTO", 13000)
    cm = ContextManager(FakeProvider(), tmp_path, 5000)
    first = _run(cm.before_request(Conversation(), None, []))
    second = _run(cm.before_request(Conversation(), None, []))
    assert first.notice
    assert second.notice == ""


# ---------------------------------------------------------------- 压缩


def test_compress_builds_summary_message(tmp_path, small):
    conv = _conv(6)
    provider = FakeProvider([GOOD_SUMMARY])
    cm = ContextManager(provider, tmp_path, 1000)

    report = _run(cm.before_request(conv, None, ["read_file", "bash"]))

    assert report.summarized > 0
    assert report.failed == ""
    first = conv.messages()[0]
    assert first.role == "user"
    assert "<context_summary>" in first.content
    assert "已写入/修改的文件" not in first.content  # 本例没有写文件
    assert "当前可用工具" in first.content
    assert "不要依据摘要推断代码" in first.content


def test_compress_report_has_token_delta(tmp_path, small):
    conv = _conv(6)
    before = len(conv.messages())
    cm = ContextManager(FakeProvider([GOOD_SUMMARY]), tmp_path, 1000)
    report = _run(cm.before_request(conv, None, []))

    assert report.info is not None
    assert report.info.phase == "done"
    assert report.info.tokens_before > report.info.tokens_after
    # 被摘要的 + 保留的 = 压缩前的条数；压缩后历史 = 1 条摘要 + 保留的
    assert report.info.summarized + report.info.kept == before
    assert report.info.kept + 1 == len(conv.messages())
    assert "tokens" in report.notice


def test_history_valid_after_compress(tmp_path, small):
    conv = _conv(6)
    cm = ContextManager(FakeProvider([GOOD_SUMMARY]), tmp_path, 1000)
    _run(cm.before_request(conv, None, []))

    msgs = conv.messages()
    assert msgs[0].role != "tool"
    pending: set[str] = set()
    for m in msgs:
        if m.role == "assistant" and m.tool_calls:
            pending = {tc.id for tc in m.tool_calls}
        elif m.role == "tool":
            assert m.tool_call_id in pending


def test_spill_runs_before_summary(tmp_path, small, monkeypatch):
    monkeypatch.setattr(budget.spill, "TOOL_RESULT_MAX", 50)
    monkeypatch.setattr(budget.spill, "BATCH_RESULT_MAX", 10_000)
    conv = _conv(3, 400)
    cm = ContextManager(FakeProvider([GOOD_SUMMARY]), tmp_path, 1000)

    report = _run(cm.before_request(conv, None, []))

    assert report.spilled == 3
    # 落盘后的预览已进历史，原文不在
    assert all("y" * 400 not in m.content for m in conv.messages())


# ---------------------------------------------------------------- 失败与熔断


def test_fuse_after_three_failures(tmp_path, small):
    conv = _conv(6)
    provider = FakeProvider([ValueError("boom")] * 5)
    cm = ContextManager(provider, tmp_path, 1000)

    for _ in range(3):
        report = _run(cm.before_request(conv, None, []))
        assert report.failed

    assert cm.fuse_blown
    assert cm.consecutive_failures >= 3


def test_fuse_blocks_auto_but_not_manual(tmp_path, small):
    conv = _conv(6)
    failing = FakeProvider([ValueError("boom")] * 3)
    cm = ContextManager(failing, tmp_path, 1000)
    for _ in range(3):
        _run(cm.before_request(conv, None, []))

    calls = failing.call_count
    report = _run(cm.before_request(conv, None, []))
    assert failing.call_count == calls  # 熔断后不再调 LLM
    assert "熔断" in report.notice

    # 手动仍可执行（此时换成能成功的 provider）
    cm.provider = FakeProvider([GOOD_SUMMARY])
    manual = _run(cm.compact_now(conv, tool_names=[]))
    assert manual.summarized > 0


def test_success_resets_failures(tmp_path, small):
    conv = _conv(6)
    provider = FakeProvider([ValueError("boom"), GOOD_SUMMARY])
    cm = ContextManager(provider, tmp_path, 1000)

    _run(cm.before_request(conv, None, []))
    assert cm.consecutive_failures == 1

    _run(cm.before_request(conv, None, []))
    assert cm.consecutive_failures == 0


def test_emergency_ignores_fuse(tmp_path, small):
    conv = _conv(6)
    cm = ContextManager(FakeProvider([GOOD_SUMMARY]), tmp_path, 1000)
    cm.fuse_blown = True

    report = _run(cm.compact_now(conv, emergency=True))

    assert report.summarized > 0
    assert report.emergency


def test_cancelled_during_summary(tmp_path, small):
    conv = _conv(6)
    before = len(conv.messages())
    event = asyncio.Event()
    event.set()
    cm = ContextManager(FakeProvider([GOOD_SUMMARY]), tmp_path, 1000)

    report = _run(cm.before_request(conv, event, []))

    assert report.cancelled
    assert len(conv.messages()) == before


def test_unparseable_summary_counts_as_failure(tmp_path, small):
    conv = _conv(6)
    cm = ContextManager(FakeProvider(["没有标签"]), tmp_path, 1000)
    report = _run(cm.before_request(conv, None, []))
    assert report.failed
    assert cm.consecutive_failures == 1


# ---------------------------------------------------------------- 手动


def test_compact_now_on_empty_session(tmp_path):
    cm = ContextManager(FakeProvider(), tmp_path, 1000)
    report = _run(cm.compact_now(Conversation()))
    assert "无需压缩" in report.notice


def test_compact_now_on_short_history(tmp_path):
    conv = Conversation()
    conv.add_user("hi")
    cm = ContextManager(FakeProvider(), tmp_path, 1000)
    report = _run(cm.compact_now(conv))
    assert "无需压缩" in report.notice


def test_next_cut_moves_to_next_user(monkeypatch):
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_TOKENS", 500)
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_MIN_MSGS", 2)
    conv = _conv(3, 400)
    msgs = conv.messages()
    cut = summarize_mod.find_cut(msgs)
    assert cut is not None and cut < len(msgs) - 1
    nxt = summarize_mod.next_cut(msgs, cut)
    assert nxt is not None and nxt > cut
    assert msgs[nxt].role == "user"


# ---------------------------------------------------------------- 触发余量与提示文案


def test_auto_skips_below_threshold_but_manual_forces(tmp_path, monkeypatch):
    """同一历史：自动余量 13K 未到线不压，手动 /compact 仍强制压（余量 3K）。"""
    monkeypatch.setattr(budget, "SAFETY_AUTO", 10_000)
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_TOKENS", 100)
    monkeypatch.setattr(summarize_mod, "KEEP_TAIL_MIN_MSGS", 2)
    conv = _conv(6)
    provider = FakeProvider([GOOD_SUMMARY])
    cm = ContextManager(provider, tmp_path, 100_000)  # 触发线 90,000，远未到

    auto = _run(cm.before_request(conv, None, []))
    assert auto.summarized == 0
    assert provider.call_count == 0

    manual = _run(cm.compact_now(conv, tool_names=[]))
    assert manual.summarized > 0
    assert provider.call_count == 1


def test_notice_has_counts_and_thousands_separator(tmp_path, small):
    """完成提示必须带统计与千分位（用户要能一眼看出这次压缩值不值）。"""
    conv = _conv(6)
    cm = ContextManager(FakeProvider([GOOD_SUMMARY]), tmp_path, 1000)
    cm.after_stream(TokenUsage(input_tokens=150_000), 0)  # 制造大数字

    report = _run(cm.compact_now(conv, tool_names=[]))

    assert re.search(r"摘要 \d+ 条 / 保留 \d+ 条", report.notice)
    assert re.search(r"tokens \d{1,3}(?:,\d{3})+ → \d{1,3}(?:,\d{3})+", report.notice)


def test_draft_not_in_history(tmp_path, small):
    """<draft> 只用于模型自检，压缩后历史里一次都不该出现。"""
    conv = _conv(6)
    raw = "<draft>草稿内容XYZ</draft><summary>" + "正式内容。" * 20 + "</summary>"
    cm = ContextManager(FakeProvider([raw]), tmp_path, 1000)

    _run(cm.before_request(conv, None, []))

    assert all("草稿内容XYZ" not in m.content for m in conv.messages())
    assert any("正式内容" in m.content for m in conv.messages())
