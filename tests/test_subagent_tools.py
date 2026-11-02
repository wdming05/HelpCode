"""工具过滤与 Agent 工具。"""

import asyncio
from pathlib import Path

from helpcode.subagent.catalog import Role, load_catalog
from helpcode.subagent.tools import (
    BACKGROUND_ALLOWED,
    GLOBAL_DISALLOWED,
    AgentTool,
    filter_registry,
)
from helpcode.tool import Registry, ToolResult, new_default_registry


class _Dummy:
    def __init__(self, name: str):
        self.name = name
        self.description = ""
        self.parameters = {"type": "object", "properties": {}}
        self.read_only = False

    def run(self, **kwargs):
        return ToolResult(ok=True, content=self.name)


def _base_with_agent() -> Registry:
    base = new_default_registry()
    base.register(_Dummy("Agent"))
    return base


def test_global_disallows_agent():
    out = filter_registry(_base_with_agent(), Role("x", "d", "b"), False)
    assert "Agent" in GLOBAL_DISALLOWED
    assert out.get("Agent") is None


def test_role_tools_allow():
    role = Role("x", "d", "b", tools=("read_file",))
    out = filter_registry(new_default_registry(), role, False)
    assert out.names() == ["read_file"]


def test_role_disallowed():
    role = Role("x", "d", "b", disallowed_tools=("bash",))
    out = filter_registry(new_default_registry(), role, False)
    assert "bash" not in out.names()


def test_background_whitelist():
    role = Role("x", "d", "b")
    out = filter_registry(new_default_registry(), role, True)
    assert set(out.names()) <= BACKGROUND_ALLOWED


def test_subset_no_mutation():
    base = new_default_registry()
    before = set(base.names())
    base.subset({"read_file"})
    assert set(base.names()) == before


class _FakeRunner:
    def __init__(self, catalog, result=None):
        self.catalog = catalog
        self._result = result or {"status": "completed", "result": "done"}
        self.seen = []

    async def run(self, role, prompt, *, background, name="", model_override=""):
        self.seen.append(
            {
                "role": role.name,
                "prompt": prompt,
                "background": background,
                "name": name,
                "model": model_override,
            }
        )
        return self._result


def _run(coro):
    return asyncio.run(coro)


def test_agent_tool_schema():
    assert set(AgentTool.parameters["required"]) == {"prompt", "description"}
    assert AgentTool.name == "Agent"


def test_unknown_subagent_type():
    cat, _ = load_catalog(Path("."), Path("."))
    runner = _FakeRunner(cat)
    res = _run(
        AgentTool(runner).arun(prompt="x", description="d", subagent_type="nope")
    )
    assert not res.ok and "未知" in res.content


def test_empty_type_fork():
    cat, _ = load_catalog(Path("."), Path("."))
    runner = _FakeRunner(cat)
    res = _run(AgentTool(runner).arun(prompt="x", description="d"))
    assert res.ok
    assert runner.seen[0]["role"] == "fork"


def test_background_returns():
    cat, _ = load_catalog(Path("."), Path("."))
    runner = _FakeRunner(cat, {"status": "async_launched", "task_id": "task-1"})
    res = _run(
        AgentTool(runner).arun(
            prompt="x", description="d", subagent_type="explore", run_in_background=True
        )
    )
    assert res.ok and "async_launched" in res.content
    assert runner.seen[0]["background"] is True
