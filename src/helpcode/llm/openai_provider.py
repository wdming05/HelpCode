"""OpenAI 协议适配器。"""

import asyncio
import json
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from ..config import ProviderConfig
from . import Request, StreamEvent, TokenUsage, ToolCall
from .errors import ContextLengthExceeded, is_context_too_long


def _to_openai_messages(req: Request) -> list[dict]:
    """把 Request 转成 openai 的 messages 格式（单条 system + 尾部 user reminder）。"""
    system_content = req.system.stable
    if req.system.environment:
        system_content += "\n\n" + req.system.environment
    result = [{"role": "system", "content": system_content}]
    for m in req.messages:
        if m.role == "tool":
            result.append(
                {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
            )
        elif m.role == "assistant" and m.tool_calls:
            result.append(
                {
                    "role": "assistant",
                    # 有 tool_calls 时 content 必须是 null，不能是空字符串。
                    # 传 "" 会让部分兼容服务端（实测 qwen/dashscope）在后续轮次
                    # 模仿「只说半句就停」——模型输出「我将要修改…」然后不再调工具。
                    "content": m.content or None,
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
    if req.reminder:
        result.append({"role": "user", "content": req.reminder})
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

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        """发起一轮流式对话，yield 文本增量与工具调用。thinking 字段忽略。"""
        kwargs = {
            "model": self._model,
            "messages": _to_openai_messages(req),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.tools:
            kwargs["tools"] = _to_openai_tools(req.tools)

        try:
            stream = await self._client.chat.completions.create(**kwargs)

            # 按 index 累积分片的工具调用
            tool_calls_map: dict[int, dict] = {}
            usage: TokenUsage | None = None

            async for chunk in stream:
                if chunk.usage:
                    details = getattr(chunk.usage, "prompt_tokens_details", None)
                    usage = TokenUsage(
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                        cache_write=0,
                        cache_read=getattr(details, "cached_tokens", 0) or 0,
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
            # 上下文超限是可恢复的：包成哨兵，让 agent 走「紧急压缩 → 重试」。
            # 其余异常原样投递，行为与引入 ch08 之前完全一致。
            yield StreamEvent(
                err=ContextLengthExceeded(str(e)) if is_context_too_long(e) else e
            )
