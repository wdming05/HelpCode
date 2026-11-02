"""恢复段单测。"""

from helpcode.context.resume import build_resume
from helpcode.llm import Message, ToolCall


def _call(call_id: str, name: str, **kwargs) -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, input=kwargs)],
    )


def test_resume_lists_written_files_in_first_seen_order():
    msgs = [
        Message(role="user", content="改两个文件"),
        _call("c1", "write_file", path="src/a.py"),
        _call("c2", "edit_file", path="src/b.py"),
        _call("c3", "edit_file", path="src/a.py"),  # 重复路径
    ]
    text = build_resume(msgs, ["read_file", "edit_file"])
    assert "src/a.py" in text
    assert "src/b.py" in text
    assert text.count("src/a.py") == 1
    assert text.index("src/a.py") < text.index("src/b.py")


def test_resume_ignores_read_only_tools():
    msgs = [
        _call("c1", "read_file", path="src/a.py"),
        _call("c2", "grep", pattern="foo"),
    ]
    text = build_resume(msgs, ["read_file"])
    assert "src/a.py" not in text
    assert "已写入" not in text


def test_resume_lists_tools_in_given_order():
    text = build_resume([], ["read_file", "glob", "grep"])
    assert "read_file / glob / grep" in text


def test_resume_has_boundary_hint_always():
    assert "不要依据摘要推断代码" in build_resume([], [])
    assert "不要依据摘要推断代码" in build_resume([], ["read_file"])


def test_resume_is_short():
    """恢复段不能变成另一个摘要：保持在几百字符内。"""
    msgs = [_call(f"c{i}", "write_file", path=f"src/mod{i}.py") for i in range(50)]
    text = build_resume(msgs, ["read_file"] * 20)
    assert len(text) < 2000
