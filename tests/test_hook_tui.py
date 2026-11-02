"""TUI 接入 Hook：UserPromptSubmit 拦截 / SessionStart / SessionEnd。"""

import asyncio
import tempfile

from null_context import NullContext

from helpcode.agent import Agent
from helpcode.command import build_default_registry
from helpcode.config import ProviderConfig
from helpcode.hook import HookEngine, HookEvent
from helpcode.hook.schema import Action, Hook, HookResult
from helpcode.llm import StreamEvent
from helpcode.permission import Mode, new_engine
from helpcode.tool import Registry
from helpcode.tui import SessionState
from helpcode.tui.app import HelpCodeApp

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


def _hook(event):
    return Hook(
        name=f"h-{event.value}",
        event=event,
        condition=None,
        action=Action("prompt", prompt="p"),
    )


def _engine(runner, events):
    return HookEngine([_hook(e) for e in events], runner=runner)


def _setup(app, fp):
    app.provider = fp
    engine, _ = new_engine(tempfile.mkdtemp())
    app.engine = engine
    app.agent = Agent(fp, Registry(), "dev", engine, NullContext())
    app.commands = build_default_registry()
    app.mode = Mode.DEFAULT


def test_user_prompt_submit_blocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def runner(hook, payload):
        if hook.event is HookEvent.USER_PROMPT_SUBMIT:
            return HookResult(block_reason="不许说这个")
        return HookResult()

    app = HelpCodeApp(_CFG, hooks=_engine(runner, [HookEvent.USER_PROMPT_SUBMIT]))
    fp = FakeProvider()

    async def _run():
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            app.state = SessionState.IDLE
            await app.submit("你好")
            assert fp.n == 0  # 未送模型
            assert app.conv.messages() == []  # 未入历史
            log = "\n".join(str(line) for line in app.query_one("#log").lines)
            assert "拦截" in log and "不许说这个" in log

    asyncio.run(_run())


def test_session_start_fires(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []

    async def runner(hook, payload):
        seen.append(hook.event)
        return HookResult()

    app = HelpCodeApp(_CFG, hooks=_engine(runner, [HookEvent.SESSION_START]))

    async def _run():
        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0)
            assert HookEvent.SESSION_START in seen

    asyncio.run(_run())


def test_clear_fires_session_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []

    async def runner(hook, payload):
        seen.append(hook.event)
        return HookResult()

    app = HelpCodeApp(
        _CFG, hooks=_engine(runner, [HookEvent.SESSION_END, HookEvent.SESSION_START])
    )

    async def _run():
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, FakeProvider())
            app.state = SessionState.IDLE
            await app.submit("/clear")
            await pilot.pause()
            assert HookEvent.SESSION_END in seen

    asyncio.run(_run())


def test_resume_fires(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []

    async def runner(hook, payload):
        seen.append(hook.event)
        return HookResult()

    app = HelpCodeApp(_CFG, hooks=_engine(runner, [HookEvent.SESSION_RESUME]))

    import json

    from helpcode.memory import session as ses

    sid = "20261020-100000-aaaa"
    ses.ensure_session_dir(tmp_path, sid)
    jsonl = tmp_path / ".helpcode" / "sessions" / sid / "conversation.jsonl"
    jsonl.write_text(
        json.dumps({"role": "user", "content": "hi", "ts": "x", "model": "m"}) + "\n",
        encoding="utf-8",
    )

    class _Meta:
        session_id = sid

    _Meta.jsonl = jsonl

    async def _run():
        async with app.run_test() as pilot:
            await pilot.pause()
            app._apply_resume(_Meta)
            await asyncio.sleep(0)
            await pilot.pause()
            assert HookEvent.SESSION_RESUME in seen

    asyncio.run(_run())
