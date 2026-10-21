"""LLM 协议层：协议无关的 Provider 抽象与统一消息/事件类型。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..config import ProviderConfig
from ..tool import ToolResult


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
    done: bool = False  # 本轮正常结束
    err: Exception | None = None  # 出错（与 done 互斥）


class Provider(Protocol):
    """协议无关的 provider 接口。"""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    # tools 为 Registry.export() 的统一格式，各适配器内部转成本协议格式
    def stream(
        self, msgs: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[StreamEvent]: ...


def new_provider(cfg: ProviderConfig) -> Provider:
    """按 cfg.protocol 构造对应适配器。"""
    if cfg.protocol == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)
    if cfg.protocol == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(cfg)
    raise ValueError(f"未知协议: {cfg.protocol}")
