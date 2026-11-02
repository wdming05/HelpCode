"""MCP 适配层测试。覆盖 AC6/AC7/AC9/F7/F8/F10。"""

import asyncio
from types import SimpleNamespace

import mcp
import pytest

from helpcode.mcp.tool import RemoteTool, build_remote_tools
from helpcode.tool import AsyncTool

McpError = getattr(mcp, "McpError", RuntimeError)


def _mcp_error(msg: str) -> BaseException:
    """构造一个真实的协议层异常（McpError 需要 ErrorData，不能直接传 str）。"""
    try:
        from mcp.types import ErrorData

        return McpError(ErrorData(code=-32000, message=msg))
    except Exception:
        return RuntimeError(msg)


class FakeText:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeImage:
    type = "image"


class FakeResult:
    def __init__(self, content=None, is_error: bool = False) -> None:
        self.content = content or []
        self.isError = is_error


class FakeSession:
    """记录调用参数；可按需抛异常或延迟返回。"""

    def __init__(self, result=None, exc: BaseException | None = None, delay=0.0):
        self.result = result or FakeResult()
        self.exc = exc
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.result


def _mcp_tool(name="echo", description="回声", schema=None, read_only=None):
    annotations = None if read_only is None else SimpleNamespace(readOnlyHint=read_only)
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=schema
        if schema is not None
        else {"type": "object", "properties": {}},
        annotations=annotations,
    )


def _build(tools, session=None, server="echo"):
    return build_remote_tools(server, session or FakeSession(), tools)


def test_name_with_illegal_chars_is_skipped(capsys):
    tools = _build([_mcp_tool(name="ok_tool"), _mcp_tool(name="bad.tool")])
    assert [t.name for t in tools] == ["mcp__echo__ok_tool"]
    assert "bad.tool" in capsys.readouterr().err


def test_prefix_format_and_async_protocol():
    (tool,) = _build([_mcp_tool()])
    assert tool.name == "mcp__echo__echo"
    assert isinstance(tool, AsyncTool)  # 有 arun 即满足协议


def test_description_fallback_contains_server():
    (tool,) = _build([_mcp_tool(description="   ")])
    assert tool.description.strip()
    assert "echo" in tool.description


def test_schema_fallback_when_missing_or_not_dict():
    for schema in (None, {}, "not-a-dict"):
        t = SimpleNamespace(
            name="t", description="d", inputSchema=schema, annotations=None
        )
        (tool,) = _build([t])
        assert tool.parameters["type"] == "object"


def test_schema_passthrough_when_valid():
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    (tool,) = _build([_mcp_tool(schema=schema)])
    assert tool.parameters == schema


def test_read_only_only_when_hint_true():
    got = [_build([_mcp_tool(read_only=v)])[0].read_only for v in (True, False, None)]
    assert got == [True, False, False]


def test_arun_joins_text_in_order():
    session = FakeSession(FakeResult([FakeText("a"), FakeText("b")]))
    (tool,) = _build([_mcp_tool()], session)
    result = asyncio.run(tool.arun(text="hi"))
    assert result.ok and not result.is_error
    assert result.content == "a\nb"


def test_arun_maps_is_error():
    session = FakeSession(FakeResult([FakeText("boom")], is_error=True))
    (tool,) = _build([_mcp_tool()], session)
    result = asyncio.run(tool.arun())
    assert result.is_error and not result.ok
    assert "boom" in result.content


def test_arun_empty_args_passes_empty_dict():
    session = FakeSession(FakeResult([FakeText("ok")]))
    (tool,) = _build([_mcp_tool()], session)
    asyncio.run(tool.arun())
    name, arguments = session.calls[0]
    assert name == "echo"
    assert arguments == {} and arguments is not None


@pytest.mark.parametrize(
    "exc",
    [_mcp_error("boom"), ConnectionError("closed"), ValueError("weird")],
)
def test_arun_protocol_errors_become_tool_error(exc):
    session = FakeSession(exc=exc)
    (tool,) = _build([_mcp_tool()], session)
    result = asyncio.run(tool.arun())
    assert result.is_error and not result.ok
    assert "调用失败" in result.content


def test_arun_timeout(monkeypatch):
    session = FakeSession(FakeResult([FakeText("late")]), delay=0.2)
    (tool,) = _build([_mcp_tool()], session)
    monkeypatch.setattr(tool, "_timeout", 0.01)
    result = asyncio.run(tool.arun())
    assert result.is_error
    assert "超时" in result.content


def test_non_text_dropped_and_warned_once(capsys):
    session = FakeSession(FakeResult([FakeText("a"), FakeImage()]))
    (tool,) = _build([_mcp_tool()], session)
    asyncio.run(tool.arun())
    asyncio.run(tool.arun())
    assert capsys.readouterr().err.count("非文本") == 1


def test_cancellation_propagates():
    session = FakeSession(exc=asyncio.CancelledError())
    (tool,) = _build([_mcp_tool()], session)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tool.arun())


def test_run_raises_runtime_error():
    (tool,) = _build([_mcp_tool()])
    with pytest.raises(RuntimeError):
        tool.run()


def test_remote_tool_direct_construction():
    tool = RemoteTool(
        server="s",
        remote_name="t",
        name="mcp__s__t",
        description="d",
        parameters={},
        read_only=False,
        session=FakeSession(),
    )
    assert tool.name == "mcp__s__t"
