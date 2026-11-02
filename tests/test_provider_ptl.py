"""provider 适配层：把 SDK 的上下文超长异常包装成 ContextLengthExceeded。

这是「撞墙重试」的信号来源。没有这一层，``is_context_too_long`` 永远没人调，
agent 侧的 ``except ContextLengthExceeded`` 也就永远收不到事件。

用假 client 直接让 SDK 抛错，不联网。
"""

import asyncio

from helpcode.config import ProviderConfig
from helpcode.llm import ContextLengthExceeded, Message, Request, System
from helpcode.llm.anthropic_provider import AnthropicProvider
from helpcode.llm.openai_provider import OpenAIProvider

OPENAI_PTL = (
    "Error code: 400 - {'error': {'message': \"This model's maximum context length "
    'is 8192 tokens. However, you requested 9000 tokens", '
    "'code': 'context_length_exceeded'}}"
)
ANTHROPIC_PTL = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'prompt is too long: 250000 tokens > 200000 maximum'}}"
)


def _req() -> Request:
    return Request(
        messages=[Message(role="user", content="hi")],
        tools=None,
        system=System(stable="s", environment=""),
        reminder="",
    )


def _drain(provider) -> list:
    async def run():
        return [ev async for ev in provider.stream(_req())]

    return asyncio.run(run())


def _openai(monkeypatch, exc: Exception) -> OpenAIProvider:
    cfg = ProviderConfig(
        name="o", protocol="openai", api_key="k", model="m", base_url="http://x"
    )
    provider = OpenAIProvider(cfg)

    async def boom(**kwargs):
        raise exc

    monkeypatch.setattr(provider._client.chat.completions, "create", boom)
    return provider


def _anthropic(monkeypatch, exc: Exception) -> AnthropicProvider:
    cfg = ProviderConfig(name="a", protocol="anthropic", api_key="k", model="m")
    provider = AnthropicProvider(cfg)

    # anthropic 走 `async with client.messages.stream(...)`，
    # 异常要在 __aenter__ 里抛才会落进 provider 的 except 分支
    class Boom:
        async def __aenter__(self):
            raise exc

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(provider._client.messages, "stream", lambda **kw: Boom())
    return provider


def test_openai_ptl_becomes_sentinel(monkeypatch):
    events = _drain(_openai(monkeypatch, RuntimeError(OPENAI_PTL)))
    assert len(events) == 1
    assert isinstance(events[0].err, ContextLengthExceeded)


def test_anthropic_ptl_becomes_sentinel(monkeypatch):
    events = _drain(_anthropic(monkeypatch, RuntimeError(ANTHROPIC_PTL)))
    assert len(events) == 1
    assert isinstance(events[0].err, ContextLengthExceeded)


def test_openai_other_error_passes_through(monkeypatch):
    events = _drain(_openai(monkeypatch, RuntimeError("rate limit exceeded")))
    assert isinstance(events[0].err, RuntimeError)
    assert not isinstance(events[0].err, ContextLengthExceeded)


def test_anthropic_other_error_passes_through(monkeypatch):
    events = _drain(_anthropic(monkeypatch, ValueError("boom")))
    assert isinstance(events[0].err, ValueError)
    assert not isinstance(events[0].err, ContextLengthExceeded)


def test_sentinel_keeps_original_message(monkeypatch):
    """消息要保留，否则用户看不到服务端说了什么。"""
    events = _drain(_openai(monkeypatch, RuntimeError(OPENAI_PTL)))
    assert "maximum context length" in str(events[0].err)
