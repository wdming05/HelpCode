"""点对点邮箱：一个收件人一个 ``inbox.json``。

为什么按收件人分文件：投递时只需要锁住**收件人**这一个文件，给十个人发消息
不会互相阻塞；所有人的消息塞一个文件则会变成全局串行。

为什么不实时推送：队员是「轮询式」的——每轮循环开头取未读。实时流式通信
需要长连接与跨进程唤醒，属于 spec 明确不做的范围。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .state import locked_json, read_json

# 协议类型：普通文本 + 四种结构化协商
MESSAGE_TYPES = (
    "text",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_request",
    "plan_approval_response",
    "idle_notification",
)

INBOX_NAME = "inbox.json"


@dataclass
class Message:
    """一条消息。正文与结构化字段共存——模型永远只需要读 ``content``。"""

    sender: str
    content: str
    id: str = ""
    timestamp: float = 0.0  # 0 → 落盘时自动补
    read: bool = False
    summary: str = ""
    type: str = "text"
    request_id: str = ""
    # 可空布尔：None = 没表态，False = 明确拒绝。用 False 当默认会把
    # 「还没回复」误判成「驳回」。
    approve: bool | None = None
    feedback: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sender": self.sender,
            "content": self.content,
            "timestamp": self.timestamp,
            "read": self.read,
            "summary": self.summary,
            "type": self.type,
            "request_id": self.request_id,
            "approve": self.approve,
            "feedback": self.feedback,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Message:
        approve = raw.get("approve", None)
        return cls(
            sender=str(raw.get("sender") or "unknown"),
            content=str(raw.get("content") or ""),
            id=str(raw.get("id") or ""),
            timestamp=float(raw.get("timestamp") or 0.0),
            read=bool(raw.get("read", False)),
            summary=str(raw.get("summary") or ""),
            type=str(raw.get("type") or "text"),
            request_id=str(raw.get("request_id") or ""),
            approve=approve if isinstance(approve, bool) else None,
            feedback=str(raw.get("feedback") or ""),
        )

    def render(self) -> str:
        """给人/模型看的一行摘要（未读注入用）。"""
        head = f"[{self.type}] {self.sender} → 你"
        parts = [head, self.content]
        if self.request_id:
            parts.append(f"(request_id={self.request_id})")
        if self.approve is not None:
            parts.append("结论：" + ("批准" if self.approve else "驳回"))
        if self.feedback:
            parts.append(f"反馈：{self.feedback}")
        return "\n".join(p for p in parts if p)


@dataclass
class Inbox:
    """收件箱文件结构。"""

    messages: list[Message] = field(default_factory=list)


class Mailbox:
    """团队邮箱。``root`` 是 ``<team>/mailboxes``。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def inbox_path(self, member: str) -> Path:
        return self.root / member / INBOX_NAME

    def send(self, recipient: str, msg: Message) -> Message:
        """投递给一个人；返回补全了 id / 时间戳的那条。"""
        if not msg.id:
            msg.id = f"msg-{uuid.uuid4().hex[:8]}"
        if not msg.timestamp:
            msg.timestamp = time.time()
        msg.read = False
        path = self.inbox_path(recipient)
        with locked_json(path) as data:
            raw = data.get("messages")
            messages = raw if isinstance(raw, list) else []
            messages.append(msg.to_dict())
            data["messages"] = messages
        return msg

    def broadcast(self, recipients, msg: Message) -> int:
        """广播；每个人收到的是独立一条（各自的已读状态互不影响）。"""
        sent = 0
        for recipient in dict.fromkeys(recipients):  # 去重且保序
            copy = Message.from_dict(msg.to_dict())
            self.send(recipient, copy)
            sent += 1
        return sent

    def peek_unread(self, member: str) -> list[Message]:
        data = read_json(self.inbox_path(member))
        raw = data.get("messages")
        items = raw if isinstance(raw, list) else []
        unread = [
            Message.from_dict(item)
            for item in items
            if isinstance(item, dict) and not item.get("read", False)
        ]
        return sorted(unread, key=lambda m: m.timestamp)

    def take_unread(self, member: str, limit: int = 20) -> list[Message]:
        """取出未读并**标记已读**。

        同一批只返回一次：重复注入会让模型对同一条消息反复响应。
        """
        path = self.inbox_path(member)
        if not path.exists():
            return []
        taken: list[Message] = []
        with locked_json(path) as data:
            raw = data.get("messages")
            items = raw if isinstance(raw, list) else []
            for item in sorted(
                (i for i in items if isinstance(i, dict)),
                key=lambda i: float(i.get("timestamp") or 0.0),
            ):
                if item.get("read", False):
                    continue
                if len(taken) >= limit:
                    break
                item["read"] = True
                taken.append(Message.from_dict(item))
            data["messages"] = items
        return taken

    def unread_count(self, member: str) -> int:
        return len(self.peek_unread(member))
