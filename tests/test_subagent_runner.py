"""子 Agent 运行器：定义式 / Fork / 隔离 / 轮数 / 模型覆盖。"""

import asyncio
from pathlib import Path

from null_context import NullContext

from helpcode.config import ProviderConfig
from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent, ToolCall
from helpcode.permission import Outcome, new_engine
from helpcode.subagent.catalog import Role, load_catalog
from helpcode.subagent.runner import ParentContext, SubAgentRunner
from helpcode.subagent.task import TaskManager
from helpcode.tool import Registry, ToolResult, new_default_registry


class _Provider:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "m"

    async def stream(self, req):
        self.calls += 1
        events = (
            self.rounds.pop(0) if self.rounds else [StreamEvent(text="", done=True)]
        )
        for ev in events:
            yield ev


class _FakeTool:
    def __init__(self, name="probe"):
        self.name = name
        self.read_only = True
        self.description = "probe"
        self.parameters = {"type": "object", "properties": {}}

    def run(self, **kwargs):
        return ToolResult(ok=True, content="probed")


def _text_round(text="结果"):
    return [StreamEvent(text=text), StreamEvent(done=True)]


def _tool_round(name="probe"):
    return [StreamEvent(done=True, tool_calls=[ToolCall(id="c1", name=name, input={})])]


def _runner(
    provider,
    *,
    parent_conv=None,
    registry_factory=None,
    provider_cfg=None,
    auto=5.0,
    approval=None,
):
    engine, _ = new_engine(str(Path.cwd()))
    conv = parent_conv or Conversation()
    parent = ParentContext(
        provider=provider,
        provider_cfg=provider_cfg,
        engine=engine,
        hooks=None,
        version="test",
        registry_factory=registry_factory or new_default_registry,
        context_factory=NullContext,
        conv_getter=lambda: conv,
        approval_handler=approval,
    )
    catalog, _ = load_catalog(Path("."), Path("."))
    return (
        SubAgentRunner(catalog, TaskManager(), parent, auto_background=auto),
        catalog,
        conv,
    )


def test_defined_empty_conv():
    runner, catalog, _parent = _runner(_Provider([_text_round()]))
    agent, conv = runner.build(catalog.get("general-purpose"), background=False)
    assert conv.messages() == []
    assert agent._system_prompt == catalog.get("general-purpose").body


def test_fork_inherits_history():
    parent_conv = Conversation()
    parent_conv.add_user("父用户")
    parent_conv.add_assistant("父回答")
    runner, catalog, _ = _runner(_Provider([_text_round()]), parent_conv=parent_conv)
    _agent, conv = runner.build(catalog.fork_base(), background=False)
    texts = [m.content for m in conv.messages()]
    assert "父用户" in texts and "父回答" in texts


def test_run_to_completion():
    runner, catalog, _ = _runner(_Provider([_text_round("子结果")]))
    result = asyncio.run(
        runner.run(catalog.get("general-purpose"), "做点事", background=False)
    )
    assert result["status"] == "completed"
    assert "子结果" in result["result"]


def test_isolated_conversation():
    parent_conv = Conversation()
    runner, catalog, _ = _runner(
        _Provider([_text_round("子结果")]), parent_conv=parent_conv
    )
    asyncio.run(runner.run(catalog.get("general-purpose"), "做点事", background=False))
    assert parent_conv.messages() == []  # 父对话未被污染


def test_max_turns_hint():
    registry = Registry()
    registry.register(_FakeTool())
    role = Role("probe", "d", "b", max_turns=1, permission_mode="dontAsk")
    runner, _catalog, _ = _runner(
        _Provider([_tool_round()]),
        registry_factory=lambda: registry,
    )
    result = asyncio.run(runner.run(role, "循环", background=False))
    assert "最大迭代次数" in result["result"] or "最大轮" in result["result"]


def test_model_override():
    cfg = ProviderConfig(name="p", protocol="openai", api_key="x", model="m")
    runner, catalog, _ = _runner(_Provider([_text_round()]), provider_cfg=cfg)
    agent, _conv = runner.build(
        catalog.get("general-purpose"), background=False, model_override="other-model"
    )
    assert agent.provider.model == "other-model"


def test_fork_forced_background():
    runner, catalog, _ = _runner(_Provider([_text_round()]))

    async def go():
        result = await runner.run(catalog.fork_base(), "fork 任务", background=False)
        # Fork 强制后台：等后台任务收尾，避免事件循环关闭时取消挂起任务
        await runner.tasks.drain(timeout=10)
        return result

    result = asyncio.run(go())
    assert result["status"] == "async_launched"


def test_background_no_handler():
    """后台子 Agent 没有 TUI 可问：构造时必须挂一个兜底 handler，避免 Ask 挂起。"""
    runner, catalog, _ = _runner(_Provider([_text_round()]))
    agent, _ = runner.build(catalog.get("general-purpose"), background=True)
    assert agent._approval_handler is not None


def test_background_denies_when_nobody_to_ask():
    """真后台（窗格后端独立进程）没人可问：一律拒绝，不阻塞。"""
    runner, catalog, _ = _runner(_Provider([_text_round()]))
    agent, _ = runner.build(catalog.get("general-purpose"), background=True)
    assert agent._approval_handler is not None
    assert asyncio.run(agent._approval_handler(None, "原因")) is Outcome.DENY_ONCE


def test_background_can_approve_when_tui_present():
    """in-process 后台任务（Team 队员）主 TUI 就在旁边：Ask 必须升级上去。

    这里曾把 background 当成「没人可问」→ 队员的写操作永久被拒，
    实测症状是队员一直复述「文件写入被拒绝，让我改用 bash」。
    """
    asked = []

    async def handler(call, reason):
        asked.append(reason)
        return Outcome.ALLOW_ONCE

    runner, catalog, _ = _runner(_Provider([_text_round()]), approval=handler)
    agent, _ = runner.build(
        catalog.get("general-purpose"), background=True, can_approve=True
    )
    assert (
        asyncio.run(agent._approval_handler(None, "需要写文件")) is Outcome.ALLOW_ONCE
    )
    assert asked == ["需要写文件"]
