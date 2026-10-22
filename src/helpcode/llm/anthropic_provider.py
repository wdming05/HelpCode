"""Anthropic 协议适配器。"""

import asyncio
import json
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic

from ..config import ProviderConfig
from ..prompt import SYSTEM_PROMPT
from . import Message, StreamEvent, TokenUsage, ToolCall


def _to_anthropic_messages(msgs: list[Message]) -> list[dict]:
    """把内部 Message 转成 anthropic 的 messages 格式。"""
    result = []
    for m in msgs:
        if m.role == "tool":
            # 工具结果 → user 消息带 tool_result 块
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id,
                            "content": m.content,
                        }
                    ],
                }
            )
        elif m.role == "assistant" and m.tool_calls:
            # assistant 工具调用 → tool_use 块
            result.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.input,
                        }
                        for tc in m.tool_calls
                    ],
                }
            )
        else:
            result.append({"role": m.role, "content": m.content})
    return result


def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """把统一工具定义转成 anthropic 的 tools 格式。"""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in tools
    ]


class AnthropicProvider:
    """封装 anthropic.AsyncAnthropic 的流式对话（含工具调用）。"""

    def __init__(self, cfg: ProviderConfig) -> None:
        self._name = cfg.name
        self._model = cfg.model
        self._thinking = cfg.thinking

        kwargs = {"api_key": cfg.api_key}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self._client = AsyncAnthropic(**kwargs)

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    async def stream(
        self,
        msgs: list[Message],
        tools: list[dict] | None = None,
        system_suffix: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """发起一轮流式对话，yield 文本增量与工具调用；思考增量内部丢弃。"""
        params = {
            "model": self._model,
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT + (system_suffix or ""),
            "messages": _to_anthropic_messages(msgs),
        }
        if tools:
            params["tools"] = _to_anthropic_tools(tools)
        if self._thinking:
            params["thinking"] = {"type": "enabled", "budget_tokens": 2048}

        try:
            async with self._client.messages.stream(**params) as stream:
                tool_calls: list[ToolCall] = []
                current: dict | None = None  # 当前 tool_use 的 {"id","name"}
                json_buffer = ""
                usage: TokenUsage | None = None

                async for event in stream:
                    if event.type == "content_block_start":
                        if event.content_block.type == "tool_use":
                            current = {
                                "id": event.content_block.id,
                                "name": event.content_block.name,
                            }
                            json_buffer = ""
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield StreamEvent(text=event.delta.text)
                        elif event.delta.type == "input_json_delta":
                            json_buffer += event.delta.partial_json
                    elif event.type == "content_block_stop" and current is not None:
                        try:
                            input_obj = json.loads(json_buffer) if json_buffer else {}
                        except json.JSONDecodeError:
                            input_obj = {}
                        tool_calls.append(
                            ToolCall(
                                id=current["id"],
                                name=current["name"],
                                input=input_obj,
                            )
                        )
                        current = None
                        json_buffer = ""
                    elif event.type == "message_delta" and event.usage:
                        usage = TokenUsage(
                            input_tokens=event.usage.input_tokens or 0,
                            output_tokens=event.usage.output_tokens or 0,
                        )

                if usage is not None:
                    yield StreamEvent(usage=usage)  # 独立事件，done 之前
                yield StreamEvent(done=True, tool_calls=tool_calls or None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            yield StreamEvent(err=e)
