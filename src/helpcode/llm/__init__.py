"""LLM 协议层：协议无关的 Provider 抽象与统一消息/事件类型。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..config import ProviderConfig
from ..tool import ToolResult
from .errors import (
    ContextLengthExceeded as ContextLengthExceeded,  # 显式 re-export，供 agent 使用
)
from .errors import is_context_too_long as is_context_too_long


@dataclass
class TokenUsage:
    """一轮对话的 Token 用量。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write: int = 0  # 写入缓存的 input token 数
    cache_read: int = 0  # 命中缓存的 input token 数


@dataclass
class ToolCall:
    """模型发起的一次工具调用请求。"""

    id: str  # 工具调用 id（回灌时协议需要对应）
    name: str  # 工具名
    input: dict  # 已解析的 JSON 参数对象


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall] | None = None  # assistant 请求工具时非空
    tool_call_id: str | None = None  # tool 消息对应哪个调用
    tool_results: list[ToolResult] = field(
        default_factory=list
    )  # 仅 role=="tool" 时使用


@dataclass
class StreamEvent:
    text: str = ""  # 文本增量
    tool_calls: list[ToolCall] | None = None  # done 时带出本轮全部工具调用
    usage: TokenUsage | None = None  # 独立事件：done 之前单独 yield
    done: bool = False  # 本轮正常结束
    err: Exception | None = None  # 出错（与 done 互斥）


@dataclass
class System:
    """系统提示：稳定段（可缓存）+ 环境段（不缓存）。"""

    stable: str = ""
    environment: str = ""


@dataclass
class Request:
    """一次对话请求的完整装配。"""

    messages: list[Message]
    tools: list[dict] | None = None
    system: System = field(default_factory=System)
    reminder: str = ""


class Provider(Protocol):
    """协议无关的 provider 接口。"""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def stream(self, req: Request) -> AsyncIterator[StreamEvent]: ...


def new_provider(cfg: ProviderConfig) -> Provider:
    """按 cfg.protocol 构造对应适配器。"""
    if cfg.protocol == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)
    if cfg.protocol == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(cfg)
    raise ValueError(f"未知协议: {cfg.protocol}")
