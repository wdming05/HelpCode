"""conversation 模块单测。"""

from helpcode.conversation import Conversation
from helpcode.llm import Message


def test_add_and_order():
    conv = Conversation()
    conv.add_user("你好")
    conv.add_assistant("你好！")
    conv.add_user("再来")
    msgs = conv.messages()
    assert len(msgs) == 3
    assert msgs[0].role == "user" and msgs[0].content == "你好"
    assert msgs[1].role == "assistant" and msgs[1].content == "你好！"
    assert msgs[2].role == "user" and msgs[2].content == "再来"


def test_messages_returns_copy():
    conv = Conversation()
    conv.add_user("a")
    msgs = conv.messages()
    msgs.clear()  # 修改副本不应影响内部
    assert len(conv.messages()) == 1


def test_rewrite_replaces_history():
    conv = Conversation()
    conv.add_user("old1")
    conv.add_user("old2")
    conv.rewrite([Message(role="user", content="new")])
    msgs = conv.messages()
    assert len(msgs) == 1
    assert msgs[0].content == "new"


def test_rewrite_deep_copies():
    """外部列表与 Message 都不得与内部共享实例。"""
    conv = Conversation()
    incoming = [Message(role="user", content="原文")]
    conv.rewrite(incoming)

    incoming.append(Message(role="user", content="后加的"))
    incoming[0].content = "被外部改了"

    msgs = conv.messages()
    assert len(msgs) == 1
    assert msgs[0].content == "原文"


def test_rewrite_then_append_keeps_order():
    conv = Conversation()
    conv.rewrite([Message(role="user", content="摘要")])
    conv.add_assistant("好的")
    assert [m.content for m in conv.messages()] == ["摘要", "好的"]
    assert conv.last_role() == "assistant"


# ---------------------------------------------------------------- 回调（ch09）


def test_on_append_called_per_add():
    """4 次 add → 4 次回调，且每次拿到的是刚追加的那条。"""
    seen = []
    conv = Conversation(on_append=seen.append)

    conv.add_user("u1")
    conv.add_assistant("a1")
    conv.add_assistant_tool_calls([])
    conv.add_user("u2")

    assert len(seen) == 4
    assert [m.content for m in seen[:2]] == ["u1", "a1"]
    assert seen[2].role == "assistant"


def test_on_replace_called_with_snapshot():
    """替换历史 → 回调拿到替换后的整段，且不是内部列表本身。"""
    seen = []
    conv = Conversation(on_replace=seen.append)

    conv.replace_messages([Message(role="user", content="压缩后")])

    assert len(seen) == 1
    assert [m.content for m in seen[0]] == ["压缩后"]
    assert seen[0] is not conv._messages


def test_rewrite_alias_still_calls_on_replace():
    """走 ch08 的 `rewrite()` 也必须触发 on_replace。

    漏掉的话，ch08 的压缩会静默不落档——文件里永远没有 compact 标记，
    重启后历史退回压缩前。
    """
    called = []
    conv = Conversation(on_replace=lambda msgs: called.append(msgs))

    conv.rewrite([Message(role="user", content="x")])

    assert len(called) == 1


def test_from_messages_does_not_fire_callbacks():
    """恢复构造：两个回调都不调用（否则恢复会写一行多余的 compact 标记）。"""
    appended, replaced = [], []

    conv = Conversation.from_messages(
        [Message(role="user", content="历史")],
        on_append=appended.append,
        on_replace=replaced.append,
    )

    assert appended == []
    assert replaced == []
    assert [m.content for m in conv.messages()] == ["历史"]


def test_callback_exception_propagates():
    """回调抛异常要向上抛：Writer 自己吞，Conversation 不替它兜。"""

    def boom(_msg):
        raise RuntimeError("archive failed")

    conv = Conversation(on_append=boom)
    try:
        conv.add_user("x")
    except RuntimeError as exc:
        assert "archive failed" in str(exc)
    else:
        raise AssertionError("回调异常应该向上抛")


def test_no_callback_still_works():
    """不传回调：既有行为逐字节不变（回归）。"""
    conv = Conversation()
    conv.add_user("a")
    conv.rewrite([Message(role="user", content="b")])
    assert [m.content for m in conv.messages()] == ["b"]
