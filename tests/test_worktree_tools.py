"""explicit cwd：路径解析、六个工具、Agent 注入、schema 不变。"""

import asyncio
import os
from pathlib import Path

from null_context import NullContext

from helpcode.agent import Agent
from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent, ToolCall
from helpcode.permission import new_engine
from helpcode.tool import new_default_registry
from helpcode.tool.bash import BashTool
from helpcode.tool.edit_file import EditFileTool
from helpcode.tool.glob_tool import GlobTool
from helpcode.tool.grep_tool import GrepTool
from helpcode.tool.paths import resolve_path
from helpcode.tool.read_file import ReadFileTool
from helpcode.tool.write_file import WriteFileTool


def test_resolve_path(tmp_path):
    base = str(tmp_path)
    absolute = str(tmp_path / "abs" / "y")
    assert resolve_path("a.txt", base) == Path(base) / "a.txt"
    assert resolve_path(absolute, base) == Path(absolute)
    # 没给 cwd 才回落进程目录
    assert resolve_path("a.txt").parent == Path(os.getcwd())


def test_read_file_cwd(tmp_path):
    (tmp_path / "唯一文件.txt").write_text("世界\n", encoding="utf-8")
    r = ReadFileTool().run("唯一文件.txt", cwd=str(tmp_path))
    assert r.ok and "世界" in r.content
    # 不给 cwd 时找的是进程目录，那里没有这个文件
    assert not ReadFileTool().run("唯一文件.txt").ok


def test_write_file_cwd(tmp_path):
    r = WriteFileTool().run("sub/x.txt", "hi", cwd=str(tmp_path))
    assert r.ok
    assert (tmp_path / "sub" / "x.txt").read_text(encoding="utf-8") == "hi"


def test_edit_file_cwd(tmp_path):
    (tmp_path / "e.txt").write_text("old\n", encoding="utf-8")
    r = EditFileTool().run("e.txt", "old", "new", cwd=str(tmp_path))
    assert r.ok
    assert (tmp_path / "e.txt").read_text(encoding="utf-8") == "new\n"


def test_glob_cwd(tmp_path):
    (tmp_path / "one.py").write_text("x", encoding="utf-8")
    r = GlobTool().run("*.py", cwd=str(tmp_path))
    assert r.ok and "one.py" in r.content.replace("\\", "/")


def test_grep_cwd(tmp_path):
    (tmp_path / "g.txt").write_text("needle\n", encoding="utf-8")
    r = GrepTool().run("needle", cwd=str(tmp_path))
    assert r.ok and "needle" in r.content


def test_bash_cwd(tmp_path):
    # 用「在这个目录里能写到文件」验证 cwd，比比对 pwd 输出更结实
    # （Windows 上 git-bash 的 pwd 打印的是 /tmp/... 这类 MSYS 路径）
    r = BashTool().run("echo hi > marker.txt", cwd=str(tmp_path))
    assert r.ok
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8").strip() == "hi"


def test_schema_unchanged():
    """工作目录不是模型可见参数（AC17/N1）。"""
    for tool in (
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
    ):
        assert tool.cwd_aware is True
        props = tool.parameters["properties"]
        assert "cwd" not in props
        assert "cwd" not in tool.parameters.get("required", [])
    assert set(WriteFileTool.parameters["properties"]) == {"path", "content"}


def test_agent_injects_cwd(tmp_path):
    (tmp_path / "z.txt").write_text("注入成功\n", encoding="utf-8")
    engine, _ = new_engine(str(Path.cwd()))

    class _P:
        name = "fake"
        model = "m"

    agent = Agent(
        _P(), new_default_registry(), "test", engine, NullContext(), cwd=str(tmp_path)
    )
    call = ToolCall(id="c1", name="read_file", input={"path": "z.txt"})
    result = asyncio.run(agent._execute_one(call))
    assert result.ok and "注入成功" in result.content


def test_agent_defaults_to_process_cwd():
    engine, _ = new_engine(str(Path.cwd()))

    class _P:
        name = "fake"
        model = "m"

    agent = Agent(_P(), new_default_registry(), "test", engine, NullContext())
    assert agent.cwd == os.getcwd()


def test_agent_env_shows_worktree_cwd(tmp_path):
    """进 Worktree 后环境段的「工作目录」必须跟着变，否则模型报错位置。"""
    seen: dict = {}

    class _P:
        name = "fake"
        model = "m"

        async def stream(self, req):
            seen["env"] = req.system.environment
            yield StreamEvent(text="ok")
            yield StreamEvent(done=True)

    engine, _ = new_engine(str(Path.cwd()))
    agent = Agent(
        _P(), new_default_registry(), "test", engine, NullContext(), cwd=str(tmp_path)
    )
    asyncio.run(agent.run_to_completion(Conversation(), "hi"))
    assert f"工作目录: {tmp_path}" in seen["env"].replace("\\", "\\")
