"""测试用的假 provider：按脚本返回事件，并记录收到的请求。"""

from collections.abc import AsyncIterator

from helpcode.llm import Request, StreamEvent, TokenUsage


class FakeProvider:
    """script 里每项是 ``str``（文本回复）或 ``Exception``（直接报错）。

    脚本用完后再调用返回空文本，避免 IndexError 掩盖真实问题。
    """

    def __init__(self, script: list | None = None) -> None:
        self._script = list(script or [])
        self.requests: list[Request] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        self.requests.append(req)
        item = self._script.pop(0) if self._script else ""
        if isinstance(item, list):
            # 一轮内的事件序列：字符串是文本增量，异常直接报错并终止本轮
            for sub in item:
                if isinstance(sub, Exception):
                    yield StreamEvent(err=sub)
                    return
                yield StreamEvent(text=str(sub))
            yield StreamEvent(usage=TokenUsage(input_tokens=100, output_tokens=10))
            yield StreamEvent(done=True)
            return
        if isinstance(item, Exception):
            yield StreamEvent(err=item)
            return
        yield StreamEvent(text=str(item))
        yield StreamEvent(usage=TokenUsage(input_tokens=100, output_tokens=10))
        yield StreamEvent(done=True)

    # 便于断言
    @property
    def call_count(self) -> int:
        return len(self.requests)


class FakeProviderWithText(FakeProvider):
    """每轮返回同一段文本（用于 agent 集成测试）。"""

    def __init__(self, text: str = "done") -> None:
        super().__init__()
        self._text = text

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        self.requests.append(req)
        yield StreamEvent(text=self._text)
        yield StreamEvent(usage=TokenUsage(input_tokens=100, output_tokens=10))
        yield StreamEvent(done=True)
