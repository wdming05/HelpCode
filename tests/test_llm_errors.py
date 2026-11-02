"""llm.errors：上下文超限的识别。"""

from helpcode.llm.errors import ContextLengthExceeded, is_context_too_long

# 被真实报文覆盖的六种表述；新增表述时同步补测试
TOO_LONG_MESSAGES = [
    # OpenAI
    (
        "Error code: 400 - {'error': {'message': \"This model's maximum context length "
        'is 8192 tokens. However, you requested 9000 tokens", '
        "'code': 'context_length_exceeded'}}"
    ),
    # Anthropic
    (
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'prompt is too long: 250000 tokens > 200000 maximum'}}"
    ),
    # 兼容端点的几种常见写法
    "input is too long for the model",
    "too many tokens in the request",
    "Please reduce the length of the messages.",
]


def test_detect_patterns():
    for msg in TOO_LONG_MESSAGES:
        assert is_context_too_long(RuntimeError(msg)), msg


def test_detect_case_insensitive():
    assert is_context_too_long(RuntimeError("PROMPT IS TOO LONG"))
    assert is_context_too_long(RuntimeError("Context_Length_Exceeded"))


def test_non_ptl_errors_pass_through():
    assert not is_context_too_long(ValueError("boom"))
    assert not is_context_too_long(RuntimeError("rate limit exceeded"))
    assert not is_context_too_long(RuntimeError(""))


def test_sentinel_is_ordinary_exception():
    """哨兵必须是普通 Exception：不能伪装成 CancelledError（取消逻辑会误判），
    也不能继承 BaseException（会被 `except Exception` 漏掉）。"""
    import asyncio

    assert issubclass(ContextLengthExceeded, Exception)
    assert not issubclass(ContextLengthExceeded, asyncio.CancelledError)
    assert not issubclass(ContextLengthExceeded, KeyboardInterrupt)
