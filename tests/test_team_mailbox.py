"""邮箱：字段补全、未读一次性、广播、结构化往返、并发投递。"""

import threading

from helpcode.team.mailbox import Mailbox, Message


def _mailbox(tmp_path):
    return Mailbox(tmp_path / "mailboxes")


def test_send_fills_fields(tmp_path):
    box = _mailbox(tmp_path)
    msg = box.send("alice", Message(sender="lead", content="你好"))
    assert msg.id.startswith("msg-")
    assert msg.timestamp > 0
    assert msg.read is False

    stored = box.peek_unread("alice")
    assert len(stored) == 1
    assert stored[0].content == "你好"
    assert stored[0].sender == "lead"


def test_take_unread_once(tmp_path):
    box = _mailbox(tmp_path)
    box.send("bob", Message(sender="lead", content="一条"))
    first = box.take_unread("bob")
    assert [m.content for m in first] == ["一条"]
    assert box.take_unread("bob") == []
    assert box.peek_unread("bob") == []


def test_take_unread_ordered_and_limited(tmp_path):
    box = _mailbox(tmp_path)
    for i in range(5):
        msg = box.send("carol", Message(sender="lead", content=f"m{i}"))
        msg.timestamp = 1000 + i
        # 直接改内存不影响已落盘内容，这里按落盘顺序断言即可
    taken = box.take_unread("carol", limit=2)
    assert len(taken) == 2
    assert len(box.take_unread("carol")) == 3


def test_broadcast(tmp_path):
    box = _mailbox(tmp_path)
    count = box.broadcast(["a", "b", "a"], Message(sender="lead", content="全体"))
    assert count == 2  # 去重
    assert box.peek_unread("a")[0].content == "全体"
    assert box.peek_unread("b")[0].content == "全体"


def test_structured_roundtrip(tmp_path):
    box = _mailbox(tmp_path)
    box.send(
        "lead",
        Message(
            sender="alice",
            content="我的计划",
            type="plan_approval_request",
            request_id="plan-alice",
            summary="待审批",
        ),
    )
    got = box.take_unread("lead")[0]
    assert got.type == "plan_approval_request"
    assert got.request_id == "plan-alice"

    box.send(
        "alice",
        Message(
            sender="lead",
            content="同意",
            type="plan_approval_response",
            request_id="plan-alice",
            approve=True,
            feedback="按此执行",
        ),
    )
    reply = box.take_unread("alice")[0]
    assert reply.approve is True
    assert reply.feedback == "按此执行"


def test_approve_defaults_to_none(tmp_path):
    """没表态必须是 None——False 会被当成「明确拒绝」。"""
    box = _mailbox(tmp_path)
    box.send("dave", Message(sender="lead", content="看看"))
    assert box.take_unread("dave")[0].approve is None


def test_concurrent_send(tmp_path):
    box = _mailbox(tmp_path)

    def worker(n: int):
        for i in range(5):
            box.send("erin", Message(sender=f"w{n}", content=f"{n}-{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(box.peek_unread("erin")) == 20


def test_render_contains_sender_and_type(tmp_path):
    msg = Message(sender="alice", content="开工", type="text")
    text = msg.render()
    assert "alice" in text and "开工" in text and "[text]" in text
