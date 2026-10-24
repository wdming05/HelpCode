"""会话模块：进程内维护单会话多轮历史。"""

import copy
import threading
from collections.abc import Callable

from .llm import Message, ToolCall
from .tool import ToolResult


class Conversation:
    """单会话多轮历史（user/assistant/tool 交替）。

    所有读写都在 ``self._lock`` 内：TUI 的定时刷新与 Agent 的流式任务是两个
    协程，压缩时 ``replace_messages`` 是一次整体替换 —— 加锁是廉价的正确性保险。

    **两个回调是可选的「存档挂钩」**：

    - ``on_append(msg)``：每追加一条就调一次
    - ``on_replace(msgs)``：整段历史被替换时调一次（ch08 压缩）

    回调签名里只有消息，没有「会话」「存档」这类概念 —— 这样 ``conversation``
    不需要知道记忆包存在，依赖方向保持单向。回调**在锁外**调用：Writer 会
    ``fsync``，在锁内做会把 TUI 的定时刷新一起阻塞。
    """

    def __init__(
        self,
        on_append: Callable[[Message], None] | None = None,
        on_replace: Callable[[list[Message]], None] | None = None,
    ) -> None:
        self._messages: list[Message] = []
        self._lock = threading.RLock()
        self._on_append = on_append
        self._on_replace = on_replace

    # ------------------------------------------------------------ 追加

    def add_user(self, text: str) -> None:
        self._append(Message(role="user", content=text))

    def add_assistant(self, text: str) -> None:
        self._append(Message(role="assistant", content=text))

    def add_assistant_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        """追加一条 assistant 请求工具的消息。"""
        self._append(Message(role="assistant", content="", tool_calls=tool_calls))

    def add_tool_result(self, tool_call_id: str, result: ToolResult) -> None:
        """追加一条工具结果消息（role=tool）。"""
        self._append(
            Message(
                role="tool",
                content=result.content,
                tool_call_id=tool_call_id,
                tool_results=[result],
            )
        )

    def _append(self, msg: Message) -> None:
        with self._lock:
            self._messages.append(msg)
        if self._on_append is not None:
            self._on_append(msg)

    # ------------------------------------------------------------ 读取

    def messages(self) -> list[Message]:
        """返回消息副本，避免外部直接修改内部状态。"""
        with self._lock:
            return list(self._messages)

    def last_role(self) -> str | None:
        """返回历史最后一条消息的角色；空历史返回 None。"""
        with self._lock:
            return self._messages[-1].role if self._messages else None

    # ------------------------------------------------------------ 替换

    def replace_messages(self, messages: list[Message]) -> None:
        """整体替换历史（压缩专用）。

        深拷贝而非浅拷贝：调用方可能仍持有旧列表（如组装请求时的副本），
        共享 ``Message`` 实例会让「改一边等于改另一边」。
        """
        with self._lock:
            self._messages = copy.deepcopy(messages)
        if self._on_replace is not None:
            self._on_replace(list(self._messages))

    # ch08 的 ContextManager 与 compaction 都在调 rewrite，保留为别名，
    # 免得为了改名去动 ch08 的代码与测试。
    rewrite = replace_messages

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_messages(
        cls,
        messages: list[Message],
        on_append: Callable[[Message], None] | None = None,
        on_replace: Callable[[list[Message]], None] | None = None,
    ) -> "Conversation":
        """从已有消息列表恢复一个会话，**不触发任何回调**。

        恢复必须走这条路径。若用 ``replace_messages`` 灌历史，会立刻触发
        ``on_replace`` → 写一行 compact 标记 + 整份历史，等于把「恢复」记成了
        一次「压缩」，文件白白变长一倍。
        """
        conv = cls(on_append=on_append, on_replace=on_replace)
        conv._messages = copy.deepcopy(messages)
        return conv
