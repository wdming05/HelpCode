"""会话模块：进程内维护单会话多轮历史。"""

from .llm import Message


class Conversation:
    """单会话多轮历史（user/assistant 交替）。"""

    def __init__(self) -> None:
        self._messages: list[Message] = []

    def add_user(self, text: str) -> None:
        self._messages.append(Message(role="user", content=text))

    def add_assistant(self, text: str) -> None:
        self._messages.append(Message(role="assistant", content=text))

    def messages(self) -> list[Message]:
        """返回消息副本，避免外部直接修改内部状态。"""
        return list(self._messages)
