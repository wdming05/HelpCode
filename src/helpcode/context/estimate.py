"""Token 估算：不引入 tokenizer，按字符类型分段估，宁多不少。

设计约束：估算只用于「判断要不要压缩」，不用于精确计费，
所以宁可高估 10%~30%（提前压缩），也不能低估（压缩迟迟不触发，最后被 400 打回来）。
"""

import json
import math
from dataclasses import dataclass

from ..llm import Message

# ASCII 约 4 字符 1 token；中文约 1.5 字符 1 token（偏保守，实际约 1 字 0.6~0.7 token）
ASCII_PER_TOKEN = 4.0
NON_ASCII_PER_TOKEN = 1.5

# 每条消息的角色标记与分隔符开销
MESSAGE_OVERHEAD = 4


def estimate_tokens(text: str) -> int:
    """保守估算一段文本的 token 数；空串为 0。"""
    if not text:
        return 0
    ascii_n = sum(1 for c in text if ord(c) < 128)
    other_n = len(text) - ascii_n
    return math.ceil(ascii_n / ASCII_PER_TOKEN + other_n / NON_ASCII_PER_TOKEN)


def message_tokens(m: Message) -> int:
    """估算一条消息的 token 数：content + tool_calls 的 JSON 开销 + 固定角色开销。"""
    n = estimate_tokens(m.content)
    if m.tool_calls:
        n += estimate_tokens(
            json.dumps(
                [{"name": tc.name, "input": tc.input} for tc in m.tool_calls],
                ensure_ascii=False,
            )
        )
    return n + MESSAGE_OVERHEAD


@dataclass
class Anchor:
    """上一次 API 请求的实测用量，作为估算的基准点。

    ``total_tokens`` 是服务端报告的**完整**上下文长度（含 tools 与 system），
    ``message_count`` 是当时发出请求的历史条数。之后的增量按字符估算。
    """

    total_tokens: int
    message_count: int
