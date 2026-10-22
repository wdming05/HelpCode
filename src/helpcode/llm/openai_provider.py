"""OpenAI 协议适配器。"""

import asyncio
import json
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from ..config import ProviderConfig
from ..prompt import SYSTEM_PROMPT
from . import Message, StreamEvent, TokenUsage, ToolCall


def _to_openai_messages(
    msgs: list[Message], system_suffix: str | None = None
) -> list[dict]:
    """把内部 Message 转成 openai 的 messages 格式（首条为 system）。"""
    system_content = SYSTEM_PROMPT + (system_suffix or "")
    result = [{"role": "system", "content": system_content}]
    for m in msgs:
        if m.role == "tool":
            result.append(
                {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
            )
        elif m.role == "assistant" and m.tool_calls:
            result.append(
                {
                    "role": "assistant",
                    "content": m.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.input, ensure_ascii=False),
                            },
                        }
                        for tc in m.tool_calls
                    ],
                }
            )
        else:
            result.append({"role": m.role, "content": m.content})
    return result


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """把统一工具定义转成 openai 的 tools 格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ]


class OpenAIProvider:
    """封装 openai.AsyncOpenAI 的流式对话（含工具调用）。"""

    def __init__(self, cfg: ProviderConfig) -> None:
        self._name = cfg.name
        self._model = cfg.model

        kwargs = {"api_key": cfg.api_key}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self._client = AsyncOpenAI(**kwargs)

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
        """发起一轮流式对话，yield 文本增量与工具调用。thinking 字段忽略。"""
        kwargs = {
            "model": self._model,
            "messages": _to_openai_messages(msgs, system_suffix),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)

        try:
            stream = await self._client.chat.completions.create(**kwargs)

            # 按 index 累积分片的工具调用
            tool_calls_map: dict[int, dict] = {}
            usage: TokenUsage | None = None

            async for chunk in stream:
                if chunk.usage:
                    usage = TokenUsage(
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                    )
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield StreamEvent(text=delta.content)
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        entry = tool_calls_map.setdefault(
                            tc.index, {"id": "", "name": "", "args": ""}
                        )
                        if tc.id:
                            entry["id"] = tc.id
                        if tc.function and tc.function.name:
                            entry["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            entry["args"] += tc.function.arguments

            tool_calls = []
            for idx in sorted(tool_calls_map):
                entry = tool_calls_map[idx]
                try:
                    input_obj = json.loads(entry["args"]) if entry["args"] else {}
                except json.JSONDecodeError:
                    input_obj = {}
                tool_calls.append(
                    ToolCall(id=entry["id"], name=entry["name"], input=input_obj)
                )

            if usage is not None:
                yield StreamEvent(usage=usage)  # 独立事件，done 之前
            yield StreamEvent(done=True, tool_calls=tool_calls or None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            yield StreamEvent(err=e)
