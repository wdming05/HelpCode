"""估算函数与锚点。"""

from helpcode.context.estimate import (
    ASCII_PER_TOKEN,
    NON_ASCII_PER_TOKEN,
    Anchor,
    estimate_tokens,
    message_tokens,
)
from helpcode.llm import Message, ToolCall


def test_estimate_ascii():
    assert estimate_tokens("a" * 400) == 400 // ASCII_PER_TOKEN
    assert estimate_tokens("a") == 1  # 向上取整，不返回 0


def test_estimate_chinese_is_conservative():
    """300 汉字至少算 200 token：宁可高估，不可低估。"""
    n = estimate_tokens("中" * 300)
    assert n >= 300 / NON_ASCII_PER_TOKEN
    assert n >= 200


def test_estimate_empty():
    assert estimate_tokens("") == 0


def test_estimate_mixed():
    mixed = "abc" * 40 + "中" * 60  # 120 ascii + 60 中文
    pure_ascii = estimate_tokens("a" * 120)
    assert estimate_tokens(mixed) > pure_ascii


def test_estimate_never_negative():
    for text in (" ", "\n", "\t", "中", "a"):
        assert estimate_tokens(text) >= 1


def test_message_tokens_counts_tool_calls():
    plain = Message(role="assistant", content="x" * 40)
    with_calls = Message(
        role="assistant",
        content="x" * 40,
        tool_calls=[
            ToolCall(
                id="c1", name="read_file", input={"path": "src/a.py", "limit": 200}
            )
        ],
    )
    assert message_tokens(with_calls) > message_tokens(plain)


def test_message_overhead_applies_to_empty():
    """空 content 也要有角色开销，否则空消息会被算成 0 token。"""
    assert message_tokens(Message(role="user", content="")) > 0


def test_anchor_fields():
    a = Anchor(total_tokens=1000, message_count=5)
    assert a.total_tokens == 1000
    assert a.message_count == 5
