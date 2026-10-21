"""LLM 协议层：协议无关的 Provider 抽象与统一消息/事件类型。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

from ..config import ProviderConfig


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass
class StreamEvent:
    text: str = ""  # 文本增量
    done: bool = False  # 本轮正常结束
    err: Exception | None = None  # 出错（与 done 互斥）


class Provider(Protocol):
    """协议无关的 provider 接口。"""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def stream(self, msgs: list[Message]) -> AsyncIterator[StreamEvent]: ...


def new_provider(cfg: ProviderConfig) -> Provider:
    """按 cfg.protocol 构造对应适配器。"""
    if cfg.protocol == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)
    if cfg.protocol == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(cfg)
    raise ValueError(f"未知协议: {cfg.protocol}")
