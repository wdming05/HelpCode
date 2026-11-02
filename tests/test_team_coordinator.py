"""Coordinator Mode：双锁、白名单、Agent 侧收窄与未读注入。"""

import asyncio
from pathlib import Path

from null_context import NullContext

from helpcode.agent import Agent
from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent
from helpcode.permission import new_engine
from helpcode.team.coordinator import (
    COORDINATOR_TOOLS,
    coordinator_prompt,
    is_coordinator_mode,
)
from helpcode.tool import new_default_registry


def test_two_locks():
    on = {"HELPCODE_COORDINATOR_MODE": "1"}
    assert is_coordinator_mode(True, on) is True
    # 只有环境变量、没有能力开关 → 关闭
    assert is_coordinator_mode(False, on) is False
    # 只有能力开关、没有环境变量 → 关闭
    assert is_coordinator_mode(True, {}) is False
    # 环境变量取各种真值写法
    for value in ("1", "true", "YES", "on"):
        assert is_coordinator_mode(True, {"HELPCODE_COORDINATOR_MODE": value}) is True
    for value in ("", "0", "off", "no"):
        assert is_coordinator_mode(True, {"HELPCODE_COORDINATOR_MODE": value}) is False


def test_whitelist_excludes_writes():
    assert "write_file" not in COORDINATOR_TOOLS
    assert "edit_file" not in COORDINATOR_TOOLS
    # 读类与 shell 必须在（合并分支靠 shell）
    assert {"read_file", "glob", "grep", "bash"} <= COORDINATOR_TOOLS
    # 调度类必须在
    assert {"Agent", "TeamCreate", "SendMessage", "TaskCreate"} <= COORDINATOR_TOOLS


def test_prompt_mentions_four_phases():
    text = coordinator_prompt()
    for word in ("调研", "综合", "实现", "验证"):
        assert word in text
    assert "write_file" in text  # 说明自己不能改代码


class _Capture:
    def __init__(self, rounds=1):
        self.reqs = []
        self.rounds = rounds

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "m"

    async def stream(self, req):
        self.reqs.append(req)
        yield StreamEvent(text="ok")
        yield StreamEvent(done=True)


def _agent(capture, **kwargs):
    engine, _ = new_engine(str(Path.cwd()))
    return Agent(
        capture, new_default_registry(), "test", engine, NullContext(), **kwargs
    )


def test_agent_narrows_tools():
    capture = _Capture()
    agent = _agent(capture, allowed_tools={"read_file", "bash"})
    asyncio.run(agent.run_to_completion(Conversation(), "hi"))

    names = {t["name"] for t in capture.reqs[0].tools}
    assert names == {"read_file", "bash"}


def test_agent_without_whitelist_keeps_all_tools():
    capture = _Capture()
    agent = _agent(capture)
    asyncio.run(agent.run_to_completion(Conversation(), "hi"))
    assert len(capture.reqs[0].tools) == len(new_default_registry().names())


def test_incoming_messages_injected():
    capture = _Capture(rounds=1)
    queue = [["[text] alice → 你\n需要你确认接口"]]

    def inbox():
        return queue.pop(0) if queue else []

    agent = _agent(capture, inbox=inbox)
    asyncio.run(agent.run_to_completion(Conversation(), "hi"))

    reminder = capture.reqs[0].reminder
    assert "<incoming-messages>" in reminder
    assert "需要你确认接口" in reminder


def test_no_incoming_messages_no_block():
    capture = _Capture()
    agent = _agent(capture, inbox=list)
    asyncio.run(agent.run_to_completion(Conversation(), "hi"))
    assert "<incoming-messages>" not in capture.reqs[0].reminder


def test_system_suffix_applied():
    capture = _Capture()
    agent = _agent(capture, system_suffix=coordinator_prompt())
    asyncio.run(agent.run_to_completion(Conversation(), "hi"))
    assert "协调者模式" in capture.reqs[0].system.stable


def test_system_suffix_absent_by_default():
    capture = _Capture()
    agent = _agent(capture)
    asyncio.run(agent.run_to_completion(Conversation(), "hi"))
    assert "协调者模式" not in capture.reqs[0].system.stable
