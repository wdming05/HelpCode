"""切点计算（find_cut）与摘要请求（summarize）。"""

import asyncio

import pytest
from fake_provider import FakeProvider

from helpcode.context import summarize
from helpcode.context.summarize import CompressCancelled
from helpcode.llm import ContextLengthExceeded, Message, ToolCall


def _msgs(spec: list[tuple[str, str, list[str] | None]]) -> list[Message]:
    """按 (role, content, tool_call_ids) 构造消息列表。"""
    out = []
    for role, content, calls in spec:
        if calls:
            out.append(
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id=c, name="bash", input={}) for c in calls],
                )
            )
        else:
            out.append(Message(role=role, content=content))
    return out


def test_cut_moves_off_tool_message(monkeypatch):
    """切点落在 tool 上 → 后移一位。"""
    monkeypatch.setattr(summarize, "KEEP_TAIL_TOKENS", 1)
    monkeypatch.setattr(summarize, "KEEP_TAIL_MIN_MSGS", 2)
    msgs = _msgs(
        [
            ("user", "u", None),
            ("assistant", "", ["c1"]),
            ("tool", "r", None),
            ("assistant", "done", None),
        ]
    )
    cut = summarize.find_cut(msgs)
    assert cut == 3
    assert msgs[cut].role != "tool"


def test_cut_never_splits_call_and_result(monkeypatch):
    """assistant(c1,c2) 的结果若不全在保留区，切点必须后移到它之后。"""
    monkeypatch.setattr(summarize, "KEEP_TAIL_TOKENS", 1)
    monkeypatch.setattr(summarize, "KEEP_TAIL_MIN_MSGS", 3)
    msgs = _msgs(
        [
            ("user", "u", None),
            ("assistant", "", ["c1", "c2"]),
            ("tool", "r1", None),
            ("tool", "r2", None),
            ("assistant", "done", None),
        ]
    )
    # 从尾部数 3 条 → i=2（tool r1），校正后应停在 4（assistant）
    cut = summarize.find_cut(msgs)
    assert cut == 4
    # 保留区内部的配对必须完整（没有 assistant 的 tool_calls 落在保留区而结果不在）
    kept = msgs[cut:]
    kept_ids = {m.tool_call_id for m in kept if m.role == "tool"}
    for m in kept:
        if m.role == "assistant" and m.tool_calls:
            assert {tc.id for tc in m.tool_calls} <= kept_ids


def test_cut_returns_none_when_all_must_stay(monkeypatch):
    monkeypatch.setattr(summarize, "KEEP_TAIL_TOKENS", 10_000)
    monkeypatch.setattr(summarize, "KEEP_TAIL_MIN_MSGS", 5)
    msgs = _msgs(
        [
            ("user", "u", None),
            ("assistant", "", ["c1"]),
            ("tool", "r", None),
            ("assistant", "done", None),
        ]
    )
    assert summarize.find_cut(msgs) is None


def test_cut_returns_none_on_too_few_messages():
    msgs = _msgs([("user", "u", None), ("assistant", "a", None)])
    assert summarize.find_cut(msgs) is None


def test_cut_defensive_when_result_missing(monkeypatch):
    """结果缺失（理论上的中间态）：一路后移，最终放弃而不是切出孤儿。"""
    monkeypatch.setattr(summarize, "KEEP_TAIL_TOKENS", 1)
    monkeypatch.setattr(summarize, "KEEP_TAIL_MIN_MSGS", 2)
    msgs = _msgs(
        [
            ("user", "u", None),
            ("assistant", "", ["c1", "c2"]),  # 缺 c2 的结果
            ("tool", "r1", None),
        ]
    )
    assert summarize.find_cut(msgs) is None


def test_cut_respects_token_budget(monkeypatch):
    """token 目标先到时也要凑够最少条数。"""
    monkeypatch.setattr(summarize, "KEEP_TAIL_TOKENS", 1)
    monkeypatch.setattr(summarize, "KEEP_TAIL_MIN_MSGS", 4)
    msgs = _msgs(
        [
            ("user", "u", None),
            ("assistant", "a1", None),
            ("user", "u2", None),
            ("assistant", "a2", None),
            ("user", "u3", None),
            ("assistant", "a3", None),
        ]
    )
    assert summarize.find_cut(msgs) == 2  # 尾部 4 条


# ---------------------------------------------------------------- 摘要请求

GOOD_SUMMARY = "<summary>" + "正式摘要内容。" * 20 + "</summary>"


def _plain(n: int) -> list[Message]:
    """n 条交替消息（user/assistant），用于验证丢组与编号。"""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(Message(role=role, content=f"第{i}条"))
    return out


def _run(coro):
    return asyncio.run(coro)


def test_summary_request_has_no_tools_and_no_environment():
    provider = FakeProvider([GOOD_SUMMARY])
    _run(summarize.summarize(provider, _plain(4)))

    req = provider.requests[0]
    assert req.tools is None
    assert req.system.environment == ""
    assert req.system.stable == summarize.SUMMARY_SYSTEM


def test_parse_summary_drops_draft():
    raw = "<draft>这是我的分析草稿</draft>\n" + GOOD_SUMMARY
    parsed = summarize.parse_summary(raw)
    assert parsed is not None
    assert "草稿" not in parsed
    assert parsed.startswith("正式摘要内容")


def test_parse_summary_missing_tag_returns_none():
    assert summarize.parse_summary("我直接写了一段话，没有标签") is None


def test_parse_summary_too_short_returns_none():
    assert summarize.parse_summary("<summary>太短</summary>") is None


def test_summarize_returns_none_when_unparseable():
    provider = FakeProvider(["没有标签的输出"])
    assert _run(summarize.summarize(provider, _plain(4))) is None


def test_ptl_retry_drops_oldest_group():
    provider = FakeProvider(
        [
            ContextLengthExceeded("prompt is too long"),
            ContextLengthExceeded("prompt is too long"),
            GOOD_SUMMARY,
        ]
    )
    msgs = _plain(6)
    parsed = _run(summarize.summarize(provider, msgs))

    assert parsed is not None
    assert provider.call_count == 3
    # 第二次起带上省略提示，且被丢的是最老的消息（第0条不在正文里）
    second = provider.requests[1].messages[0].content
    assert "被省略" in second
    assert "第0条" not in second
    assert "第2条" in second


def test_ptl_retry_gives_up_after_limit():
    provider = FakeProvider([ContextLengthExceeded("prompt is too long")] * 10)
    with pytest.raises(ContextLengthExceeded):
        _run(summarize.summarize(provider, _plain(10)))
    assert provider.call_count <= summarize.PTL_RETRY_LIMIT + 1


def test_truncate_before_summary():
    provider = FakeProvider([GOOD_SUMMARY])
    big = "x" * 20000  # 约 5000 token，远超 TRUNCATE_BEFORE_SUMMARY
    msgs = [
        Message(role="user", content="跑一下"),
        Message(role="tool", content=big, tool_call_id="c1"),
    ]
    _run(summarize.summarize(provider, msgs))

    chunk = provider.requests[0].messages[0].content
    assert "已截断" in chunk
    assert big not in chunk
    assert len(chunk) < len(big)


def test_cancel_during_summary():
    provider = FakeProvider([GOOD_SUMMARY])
    event = asyncio.Event()
    event.set()

    with pytest.raises(CompressCancelled):
        _run(summarize.summarize(provider, _plain(4), event))


def test_non_ptl_error_propagates():
    provider = FakeProvider([ValueError("boom")])
    with pytest.raises(ValueError, match="boom"):
        _run(summarize.summarize(provider, _plain(4)))


def test_groups_keep_call_with_its_results():
    msgs = [
        Message(role="user", content="u"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="bash", input={})],
        ),
        Message(role="tool", content="r1", tool_call_id="c1"),
        Message(role="assistant", content="a"),
    ]
    groups = summarize._groups(msgs)
    assert groups == [(0, 1), (1, 3), (3, 4)]


# ---------------------------------------------------------------- 切点只后移


def test_fix_cut_only_moves_forward_on_tool():
    """初始切点落在 tool 消息上：只能后移到 3，不能前移。"""
    msgs = [
        Message(role="user", content="u"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="bash", input={})],
        ),
        Message(role="tool", content="r1", tool_call_id="c1"),
        Message(role="assistant", content="a"),
    ]
    assert summarize._fix_cut(msgs, 2) == 3


def test_fix_cut_only_moves_forward_on_incomplete_pair():
    """初始切点落在 assistant(tool_calls) 但结果不全会跟过来：后移。"""
    msgs = [
        Message(role="user", content="u"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="c1", name="bash", input={}),
                ToolCall(id="c2", name="bash", input={}),
            ],
        ),
        Message(role="tool", content="r1", tool_call_id="c1"),
        Message(role="user", content="u2"),
    ]
    fixed = summarize._fix_cut(msgs, 1)
    assert fixed is not None and fixed > 1


def test_fix_cut_only_moves_forward_on_orphan_call():
    """孤立的 assistant(tool_calls)（结果全不在保留区）：后移。"""
    msgs = [
        Message(role="user", content="u"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c9", name="bash", input={})],
        ),
        Message(role="assistant", content="a"),
    ]
    fixed = summarize._fix_cut(msgs, 1)
    assert fixed is not None and fixed > 1


def test_fix_cut_returns_none_when_cannot_fix():
    """移到末尾仍不合法：返回 None 而不是硬返回一个非法切点。"""
    msgs = [
        Message(role="user", content="u"),
        Message(role="tool", content="r", tool_call_id="x"),
    ]
    assert summarize._fix_cut(msgs, 1) is None
