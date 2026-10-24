"""会话模块：进程内维护单会话多轮历史。"""

import copy
import threading

from .llm import Message, ToolCall
from .tool import ToolResult


class Conversation:
    """单会话多轮历史（user/assistant/tool 交替）。

    所有读写都在 ``self._lock`` 内：TUI 的定时刷新与 Agent 的流式任务是两个
    协程，压缩时 ``rewrite`` 是一次整体替换 —— 加锁是廉价的正确性保险。
    """

    def __init__(self) -> None:
        self._messages: list[Message] = []
        self._lock = threading.RLock()

    def add_user(self, text: str) -> None:
        with self._lock:
            self._messages.append(Message(role="user", content=text))

    def add_assistant(self, text: str) -> None:
        with self._lock:
            self._messages.append(Message(role="assistant", content=text))

    def add_assistant_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        """追加一条 assistant 请求工具的消息。"""
        with self._lock:
            self._messages.append(
                Message(role="assistant", content="", tool_calls=tool_calls)
            )

    def add_tool_result(self, tool_call_id: str, result: ToolResult) -> None:
        """追加一条工具结果消息（role=tool）。"""
        with self._lock:
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
        with self._lock:
            return list(self._messages)

    def rewrite(self, messages: list[Message]) -> None:
        """整体替换历史（压缩专用）。

        深拷贝而非浅拷贝：调用方可能仍持有旧列表（如组装请求时的副本），
        共享 ``Message`` 实例会让「改一边等于改另一边」。
        """
        with self._lock:
            self._messages = copy.deepcopy(messages)

    def last_role(self) -> str | None:
        """返回历史最后一条消息的角色；空历史返回 None。"""
        with self._lock:
            return self._messages[-1].role if self._messages else None
