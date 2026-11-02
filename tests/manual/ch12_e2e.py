"""ch12 端到端：真实 hooks.yaml + 真实 shell 动作 + 真实 Agent。

三个场景：
1. PreToolUse 拦截：bash 被 Hook `exit 2` 拦下，工具不执行、原因回灌模型。
2. PostToolUse：工具执行后 Hook 命令写出标记文件（旁路动作不影响主流程）。
3. only_once + async：同会话只跑一次、异步不阻塞。

运行：.venv/Scripts/python.exe tests/manual/ch12_e2e.py
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
from helpcode.hook import HookEngine, HookEvent, load_hooks
from helpcode.hook.schema import Action, Hook, HookResult
from helpcode.llm import StreamEvent, ToolCall
from helpcode.permission import Mode, new_engine
from helpcode.tool import Registry, ToolResult


class FakeTool:
    def __init__(self, name, read_only=False):
        self.name = name
        self.read_only = read_only
        self.description = "fake"
        self.parameters = {"type": "object", "properties": {}}
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        return ToolResult(ok=True, content="ran")


class FakeProvider:
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


def _agent(provider, registry, hooks):
    engine, _ = new_engine(tempfile.mkdtemp())
    return Agent(provider, registry, "e2e", engine, NullContext(), hooks=hooks)


async def _run(agent, text="hi"):
    return [
        ev async for ev in agent.run(text, Conversation(), asyncio.Event(), Mode.BYPASS)
    ]


def _write_hooks(work: Path, body: str) -> None:
    path = work / ".helpcode" / "hooks.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


async def scenario_block(work: Path) -> None:
    _write_hooks(
        work,
        """
hooks:
  - name: block-bash
    event: PreToolUse
    if:
      field: tool
      pattern: "=bash"
    action:
      type: command
      command: "echo 危险命令被禁止 1>&2 && exit 2"
    timeout: 5s
""",
    )
    hooks, warns = load_hooks(work, work / "home")
    assert len(hooks) == 1 and warns == [], (hooks, warns)
    engine = HookEngine(hooks)

    tool = FakeTool("bash", read_only=False)
    registry = Registry()
    registry.register(tool)
    provider = FakeProvider(
        [
            [
                StreamEvent(
                    done=True, tool_calls=[ToolCall(id="c1", name="bash", input={})]
                )
            ],
            [StreamEvent(text="已调整"), StreamEvent(done=True)],
        ]
    )
    events = await _run(_agent(provider, registry, engine))
    assert tool.calls == 0, "被 Hook 拦下，工具不应执行"
    ends = [e.tool_end for e in events if e.tool_end is not None]
    assert any("危险命令被禁止" in r.content for r in ends), ends
    print("  场景1 通过：PreToolUse 拦截，工具未执行、原因回灌")


async def scenario_post(work: Path) -> None:
    marker = work / "post_hit.txt"
    _write_hooks(
        work,
        """
hooks:
  - name: mark-post
    event: PostToolUse
    action:
      type: command
      command: "echo hit >> post_hit.txt"
    timeout: 5s
""",
    )
    hooks, _ = load_hooks(work, work / "home")
    engine = HookEngine(hooks)

    tool = FakeTool("grep", read_only=True)
    registry = Registry()
    registry.register(tool)
    provider = FakeProvider(
        [
            [
                StreamEvent(
                    done=True, tool_calls=[ToolCall(id="c1", name="grep", input={})]
                )
            ],
            [StreamEvent(text="完成"), StreamEvent(done=True)],
        ]
    )
    await _run(_agent(provider, registry, engine))
    assert tool.calls == 1, "PostToolUse 不应阻止工具执行"
    assert marker.exists() and "hit" in marker.read_text(encoding="utf-8")
    print("  场景2 通过：PostToolUse 命令写出标记，主流程照常")


async def scenario_once_async() -> None:
    calls = []

    async def runner(hook, payload):
        calls.append(1)
        return HookResult()

    hook = Hook(
        name="once",
        event=HookEvent.NOTIFICATION,
        condition=None,
        action=Action("prompt", prompt="x"),
        only_once=True,
    )
    engine = HookEngine([hook], runner=runner)
    await engine.dispatch(HookEvent.NOTIFICATION, {})
    await engine.dispatch(HookEvent.NOTIFICATION, {})
    assert calls == [1], "only_once 应只跑一次"

    async def slow(hook, payload):
        await asyncio.sleep(0.2)
        return HookResult()

    slow_hook = Hook(
        name="slow",
        event=HookEvent.NOTIFICATION,
        condition=None,
        action=Action("prompt", prompt="x"),
        is_async=True,
    )
    engine2 = HookEngine([slow_hook], runner=slow)
    import time

    start = time.monotonic()
    await engine2.dispatch(HookEvent.NOTIFICATION, {})
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"async 不应阻塞（{elapsed:.2f}s）"
    await engine2.drain()
    print("  场景3 通过：only_once 只跑一次、async 不阻塞")


async def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="hc-ch12-"))
    os.chdir(work)
    await scenario_block(work)
    await scenario_post(work)
    await scenario_once_async()
    print("CH12 E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
