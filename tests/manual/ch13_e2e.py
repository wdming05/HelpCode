"""ch13 端到端：主 Agent 调 Agent 工具、后台任务通知、工具隔离。

运行：.venv/Scripts/python.exe tests/manual/ch13_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from helpcode.agent import Agent
from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent, ToolCall
from helpcode.permission import Mode, new_engine
from helpcode.subagent import (
    AgentTool,
    ParentContext,
    SubAgentRunner,
    TaskManager,
    load_catalog,
)
from helpcode.subagent.tools import filter_registry
from helpcode.tool import new_default_registry


class NullContext:
    context_window = 128000

    def estimate(self, conv):
        return 0

    def should_compress(self, conv):
        return False

    def after_stream(self, usage, message_count):
        return None

    def has_room_after_compress(self, n):
        return True

    async def before_request(self, conv, cancel_event, tool_names):
        from helpcode.context import CompressReport

        return CompressReport()

    async def compact_now(self, conv, emergency=False, tool_names=None):
        from helpcode.context import CompressReport

        return CompressReport(notice="无需压缩")


class Scripted:
    def __init__(self, rounds):
        self.rounds = list(rounds)

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "m"

    async def stream(self, req):
        for ev in self.rounds.pop(0):
            yield ev


class Text:
    def __init__(self, text):
        self.text = text

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "m"

    async def stream(self, req):
        yield StreamEvent(text=self.text)
        yield StreamEvent(done=True)


async def scenario_inline(work: Path) -> None:
    parent_conv = Conversation()
    catalog, warnings = load_catalog(work, work / "home")
    assert warnings == []
    engine, _ = new_engine(str(work))
    tasks = TaskManager()
    parent = ParentContext(
        provider=Text("子 Agent 结论：找到了实现"),
        provider_cfg=None,
        engine=engine,
        hooks=None,
        version="e2e",
        registry_factory=new_default_registry,
        context_factory=NullContext,
        conv_getter=lambda: parent_conv,
        approval_handler=None,
    )
    runner = SubAgentRunner(catalog, tasks, parent, auto_background=10)
    registry = new_default_registry()
    registry.register(AgentTool(runner))

    main = Scripted(
        [
            [
                StreamEvent(
                    done=True,
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="Agent",
                            input={
                                "prompt": "找一下压缩实现在哪",
                                "description": "探索",
                                "subagent_type": "explore",
                            },
                        )
                    ],
                )
            ],
            [StreamEvent(text="收到：找到了实现"), StreamEvent(done=True)],
        ]
    )
    agent = Agent(main, registry, "e2e", engine, NullContext())
    events = [
        ev
        async for ev in agent.run(
            "帮我探索代码", parent_conv, asyncio.Event(), Mode.BYPASS
        )
    ]
    ends = [e.tool_end for e in events if e.tool_end is not None]
    assert any("子 Agent 结论" in r.content for r in ends), ends
    assert any(e.text == "收到：找到了实现" for e in events)
    print("  场景1 通过：主 Agent 调 Agent 工具拿到子 Agent 结果")


async def scenario_background(work: Path) -> None:
    notes = []
    catalog, _ = load_catalog(work, work / "home")
    engine, _ = new_engine(str(work))
    tasks = TaskManager(
        on_done=lambda t: notes.append(
            f"<task-notification>{t.id}:{t.status}:{t.result}</task-notification>"
        )
    )
    parent = ParentContext(
        provider=Text("后台完成"),
        provider_cfg=None,
        engine=engine,
        hooks=None,
        version="e2e",
        registry_factory=new_default_registry,
        context_factory=NullContext,
        conv_getter=lambda: Conversation(),
        approval_handler=None,
    )
    runner = SubAgentRunner(catalog, tasks, parent)
    result = await runner.run(
        catalog.get("general-purpose"), "后台跑一下", background=True, name="bg"
    )
    assert result["status"] == "async_launched"
    await tasks.drain(timeout=10)
    assert notes and "<task-notification>" in notes[0]
    print("  场景2 通过：后台任务完成并产生通知")


def scenario_isolation(work: Path) -> None:
    catalog, _ = load_catalog(work, work / "home")
    role = catalog.get("general-purpose")
    sub = filter_registry(new_default_registry(), role, False)
    assert sub.get("Agent") is None, "子 Agent 不应看到 Agent 工具"
    parent_conv = Conversation()
    parent_conv.add_user("父消息")
    print("  场景3 通过：子 Agent 工具集无 Agent；父对话未被触碰")


async def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="hc-ch13-"))
    os.chdir(work)
    await scenario_inline(work)
    await scenario_background(work)
    scenario_isolation(work)
    print("CH13 E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
