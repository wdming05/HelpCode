"""TUI 接入：Agent 工具注册、子 Agent 工具集无 Agent、通知注入。"""

import asyncio
from types import SimpleNamespace

from helpcode.config import ProviderConfig
from helpcode.llm import StreamEvent
from helpcode.subagent import load_catalog
from helpcode.tui import SessionState
from helpcode.tui.app import HelpCodeApp

_CFG = [ProviderConfig(name="qwen", protocol="openai", api_key="x", model="m")]


class _Provider:
    name = "fake"
    model = "m"

    async def stream(self, req):
        yield StreamEvent(text="收到")
        yield StreamEvent(done=True)


def _setup(app, fp):
    app.provider = fp
    app.provider_cfg = _CFG[0]
    app._setup_agent()


def test_agent_tool_registered(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cat, _ = load_catalog(tmp_path, tmp_path / "home")
    app = HelpCodeApp(_CFG, catalog=cat)

    async def _run():
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, _Provider())
            assert app.agent.registry.get("Agent") is not None
            assert app._base_registry().get("Agent") is None  # 子 Agent 工具池没有

    asyncio.run(_run())


def test_notice_injected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cat, _ = load_catalog(tmp_path, tmp_path / "home")
    app = HelpCodeApp(_CFG, catalog=cat)

    async def _run():
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, _Provider())
            app.state = SessionState.IDLE
            task = SimpleNamespace(
                id="task-1", name="worker", status="completed", result="子结果"
            )
            app._on_task_done(task)
            assert app._pending_notices

            await app.send_to_ai("你好")
            for _ in range(200):
                if app.state is SessionState.IDLE:
                    break
                await asyncio.sleep(0.01)
            users = [m.content for m in app.conv.messages() if m.role == "user"]
            assert any("<task-notification>" in c for c in users)
            assert app._pending_notices == []

    asyncio.run(_run())
