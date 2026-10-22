"""会话模块：进程内维护单会话多轮历史。"""

from .llm import Message, ToolCall
from .tool import ToolResult


class Conversation:
    """单会话多轮历史（user/assistant/tool 交替）。"""

    def __init__(self) -> None:
        self._messages: list[Message] = []

    def add_user(self, text: str) -> None:
        self._messages.append(Message(role="user", content=text))

    def add_assistant(self, text: str) -> None:
        self._messages.append(Message(role="assistant", content=text))

    def add_assistant_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        """追加一条 assistant 请求工具的消息。"""
        self._messages.append(
            Message(role="assistant", content="", tool_calls=tool_calls)
        )

    def add_tool_result(self, tool_call_id: str, result: ToolResult) -> None:
        """追加一条工具结果消息（role=tool）。"""
        self._messages.append(
            Message(
                role="tool",
                content=result.content,
                tool_call_id=tool_call_id,
                tool_results=[result],
            )
        )

    def messages(self) -> list[Message]:
        """返回消息副本，避免外部直接修改内部状态。"""
        return list(self._messages)

    def last_role(self) -> str | None:
        """返回历史最后一条消息的角色；空历史返回 None。"""
        return self._messages[-1].role if self._messages else None
