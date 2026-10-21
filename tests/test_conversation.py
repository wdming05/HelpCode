"""conversation 模块单测。"""

from helpcode.conversation import Conversation


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
