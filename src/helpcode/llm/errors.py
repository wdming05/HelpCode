"""LLM 层错误类型：把服务端的「上下文超限」识别为哨兵异常。

为什么按错误消息文本判定，而不是 ``isinstance(exc, openai.BadRequestError)``：

1. 兼容端点（如阿里云 OpenAI 兼容模式）的错误码不一定标准；
2. 两个 SDK 的异常类名不同，做类型判断要 import 两个第三方库，
   而本模块本身不该有依赖；
3. 文本判定失败的最坏后果是「退回普通错误上报」，与不做判定前的行为一致。
"""

# 服务端在拒绝「请求太长」时的常见表述，全部小写，按子串匹配
_PATTERNS = (
    "context_length_exceeded",
    "maximum context length",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "reduce the length",
)


class ContextLengthExceeded(Exception):
    """服务端以「上下文超限」拒绝请求（prompt too long / context_length_exceeded）。

    provider 抛出它，agent 据此走「紧急压缩 → 同迭代重试一次」的路径。
    它是哨兵：只表示「这次请求因长度被拒」，不携带任何恢复策略。
    """


def is_context_too_long(exc: Exception) -> bool:
    """判断异常是否是「上下文超限」。"""
    msg = str(exc).lower()
    return any(p in msg for p in _PATTERNS)
