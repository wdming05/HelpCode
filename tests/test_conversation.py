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
