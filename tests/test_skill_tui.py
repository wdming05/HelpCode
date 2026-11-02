"""Skill 接入 TUI：/clear 清激活、shared/isolated 执行、独立历史种子。"""

import asyncio
import tempfile

from null_context import NullContext

from helpcode.agent import Agent
from helpcode.command import build_default_registry
from helpcode.config import ProviderConfig
from helpcode.llm import StreamEvent
from helpcode.permission import Mode, new_engine
from helpcode.skill import SkillManager
from helpcode.skill.registry import SkillRegistry
from helpcode.skill.types import ExecutionMode, Skill
from helpcode.tool import Registry
from helpcode.tui import SessionState
from helpcode.tui.app import HelpCodeApp, _last_assistant, _seed_messages

_CFG = [ProviderConfig(name="qwen", protocol="openai", api_key="x", model="m")]


class FakeProvider:
    name = "fake"
    model = "m"

    def __init__(self):
        self.n = 0

    async def stream(self, req):
        self.n += 1
        yield StreamEvent(text="完成")
        yield StreamEvent(done=True)


def _iso_manager():
    reg = SkillRegistry()
    reg.set(
        Skill(
            name="iso",
            description="独立执行",
            body="回答：{{args}}",
            mode=ExecutionMode.ISOLATED,
            history=0,
        )
    )
    return SkillManager(reg)


async def _wait_state(app, state, timeout=200):
    for _ in range(timeout):
        if app.state is state:
            return True
        await asyncio.sleep(0.01)
    return False


def _base_setup(app, fp):
    app.provider = fp
    app.provider_cfg = _CFG[0]
    app.engine, _ = new_engine(tempfile.mkdtemp())
    app.agent = Agent(fp, Registry(), "dev", app.engine, NullContext())
    app.commands = build_default_registry()
    app.mode = Mode.DEFAULT
    app.state = SessionState.IDLE


def test_clear_clears_skills(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def _run():
        app = HelpCodeApp(_CFG)
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _base_setup(app, fp)
            manager = _iso_manager()
            app.skill_manager = manager
            manager.activate("iso")
            assert manager.render_active() != ""

            await app.submit("/clear")
            await pilot.pause()
            assert manager.render_active() == ""
            assert manager.active_names() == ()

    asyncio.run(_run())


def test_shared_mode_sends_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def _run():
        app = HelpCodeApp(_CFG)
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _base_setup(app, fp)
            reg = SkillRegistry()
            reg.set(Skill(name="s", description="共享", body="范围：{{args}}"))
            manager = SkillManager(reg)
            app.skill_manager = manager
            app._register_skill_command(manager.get("s"))

            await app.submit("/s src/foo")
            assert await _wait_state(app, SessionState.IDLE)
            assert fp.n == 1
            users = [m.content for m in app.conv.messages() if m.role == "user"]
            assert any("范围：src/foo" in c for c in users)

    asyncio.run(_run())


def test_isolated_returns_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def _run():
        app = HelpCodeApp(_CFG)
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _base_setup(app, fp)
            manager = _iso_manager()
            app.skill_manager = manager

            result = await app._run_skill("iso", "测试参数")
            assert "完成" in result
            # 主历史只多一条回流摘要（assistant），且不包含原始 user 消息
            msgs = app.conv.messages()
            assert len(msgs) == 1
            assert msgs[0].role == "assistant"
            assert "[Skill: iso]" in msgs[0].content
            assert app.state == SessionState.IDLE

    asyncio.run(_run())


def test_isolated_history_seed():
    from helpcode.llm import Message

    msgs = [
        Message(role="user", content="u1"),
        Message(role="assistant", content="a1"),
        Message(role="tool", content="t1", tool_call_id="x"),
        Message(role="assistant", content="", tool_calls=[]),
        Message(role="user", content="u2"),
    ]
    # 只带 user/assistant 有正文的最近 2 条
    seed = _seed_messages(msgs, 2)
    assert [m.content for m in seed] == ["a1", "u2"]
    assert _seed_messages(msgs, 0) == []


def test_last_assistant():
    from helpcode.conversation import Conversation
    from helpcode.llm import Message

    conv = Conversation.from_messages([Message(role="assistant", content="结论")])
    assert _last_assistant(conv) == "结论"
    assert _last_assistant(Conversation()) == ""
