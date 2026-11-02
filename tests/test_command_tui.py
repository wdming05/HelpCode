"""命令系统接入 TUI 的测试：分流、Tab 补全、状态栏、/clear。"""

import asyncio
import tempfile

from null_context import NullContext
from textual.widgets import OptionList, RichLog

from helpcode.agent import Agent
from helpcode.command import build_default_registry
from helpcode.config import ProviderConfig
from helpcode.llm import StreamEvent
from helpcode.permission import Mode, new_engine
from helpcode.tool import Registry
from helpcode.tui import SessionState
from helpcode.tui.app import HelpCodeApp, InputArea
from helpcode.tui.view import mode_mark

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


def _app():
    app = HelpCodeApp(_CFG)
    return app


def _setup(app, fp):
    engine, _ = new_engine(tempfile.mkdtemp())
    app.provider = fp
    app.agent = Agent(fp, Registry(), "dev", engine, NullContext())
    app.commands = build_default_registry()
    app.mode = Mode.DEFAULT


def _input(app):
    return app.query_one("#input", InputArea)


async def _wait_state(app, state, timeout=200):
    for _ in range(timeout):
        if app.state is state:
            return True
        await asyncio.sleep(0.01)
    return False


# ---------------------------------------------------------------- 分流


def test_submit_command_does_not_call_model():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            await app.submit("/status")
            await pilot.pause()
            assert fp.n == 0
            assert any(
                "模型" in str(line) for line in app.query_one("#log", RichLog).lines
            )

    asyncio.run(_run())


def test_submit_plain_text_goes_to_ai():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            await app.submit("你好")
            assert await _wait_state(app, SessionState.IDLE)
            assert fp.n == 1

    asyncio.run(_run())


def test_dispatch_error_does_not_crash():
    async def _run():
        app = _app()
        fp = FakeProvider()

        async def _boom(ctx):
            raise ValueError("坏命令")

        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            # 挂一个会抛异常的命令
            from helpcode.command.types import Command, CommandType

            app.commands.register(
                Command(
                    name="boom",
                    description="d",
                    usage="/boom",
                    type=CommandType.LOCAL,
                    handler=_boom,
                )
            )
            await app.submit("/boom")
            await pilot.pause()
            assert app.state == SessionState.IDLE
            assert any(
                "执行失败" in str(line) for line in app.query_one("#log", RichLog).lines
            )

    asyncio.run(_run())


def test_uppercase_command_same_as_lower():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            await app.submit("/PLAN")
            assert app.mode == Mode.PLAN
            await app.submit("/plan")
            assert app.mode == Mode.PLAN  # 幂等

    asyncio.run(_run())


def test_submit_ignored_when_busy():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            app.state = SessionState.STREAMING
            before = len(app.query_one("#log", RichLog).lines)
            await app.submit("/status")
            assert len(app.query_one("#log", RichLog).lines) == before

    asyncio.run(_run())


# ---------------------------------------------------------------- Tab 补全


def test_tab_single_completion():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            _input(app).text = "/st"
            app.handle_tab()
            assert _input(app).text == "/status "
            assert app.state == SessionState.IDLE

    asyncio.run(_run())


def test_tab_multi_opens_menu():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            _input(app).text = "/"
            app.handle_tab()
            assert app.state == SessionState.COMPLETING
            menu = app.query_one("#command_list", OptionList)
            assert menu.display is True
            assert menu.option_count == len(app.commands.complete("/"))

    asyncio.run(_run())


def test_tab_zero_match():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            _input(app).text = "/zz"
            before = _input(app).text
            app.handle_tab()
            assert _input(app).text == before
            assert app.state == SessionState.IDLE

    asyncio.run(_run())


def test_tab_non_slash_not_hijacked():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            _input(app).text = "abc"
            app.handle_tab()
            assert _input(app).text == "abc"
            assert app.state == SessionState.IDLE

    asyncio.run(_run())


def test_command_list_selection_fills_input():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            _input(app).text = "/"
            app.handle_tab()
            assert app.state == SessionState.COMPLETING
            app._select_command(0)
            assert app.state == SessionState.IDLE
            assert _input(app).text.startswith("/")

    asyncio.run(_run())


def test_escape_closes_command_list():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            _input(app).text = "/"
            app.handle_tab()
            assert app.state == SessionState.COMPLETING
            await app.action_cancel()
            assert app.state == SessionState.IDLE
            assert app.query_one("#command_list", OptionList).display is False

    asyncio.run(_run())


def test_menu_excludes_hidden():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            _input(app).text = "/"
            app.handle_tab()
            menu = app.query_one("#command_list", OptionList)
            ids = [
                str(menu.get_option_at_index(i).prompt)
                for i in range(menu.option_count)
            ]
            assert all(i not in ("/exit", "/quit", "/q") for i in ids)

    asyncio.run(_run())


# ---------------------------------------------------------------- 状态栏


def test_statusbar_marks_mode():
    assert mode_mark(Mode.PLAN) == "[PLAN]"
    assert mode_mark(Mode.DEFAULT) == "[DEFAULT]"


def test_statusbar_all_modes():
    expect = {
        Mode.DEFAULT: "[DEFAULT]",
        Mode.ACCEPT_EDITS: "[ACCEPT_EDITS]",
        Mode.PLAN: "[PLAN]",
        Mode.BYPASS: "[BYPASS]",
    }
    for mode, mark in expect.items():
        assert mode_mark(mode) == mark


def test_plan_command_updates_statusbar():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            await app.submit("/plan")
            await pilot.pause()
            assert app.mode == Mode.PLAN

    asyncio.run(_run())


def test_do_command_updates_statusbar():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            app.mode = Mode.PLAN
            await app.submit("/do")
            assert await _wait_state(app, SessionState.IDLE)
            assert app.mode == Mode.DEFAULT

    asyncio.run(_run())


# ---------------------------------------------------------------- /clear


def test_clear_command_replaces_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            app.session_id = "20261024-100000-aaaa"
            await app.submit("/clear")
            await pilot.pause()
            assert app.session_id is not None
            assert app.session_id != "20261024-100000-aaaa"
            assert app.conv.messages() == []
            sessions = list((tmp_path / ".helpcode" / "sessions").iterdir())
            assert len(sessions) == 1  # 只有新会话目录
            assert sessions[0].name == app.session_id

    asyncio.run(_run())


def test_compact_does_not_grow_history():
    async def _run():
        app = _app()
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            app.conv.add_user("问题")
            app.conv.add_assistant("回答")
            before = len(app.conv.messages())
            await app.submit("/compact")
            await pilot.pause()
            assert len(app.conv.messages()) <= before

    asyncio.run(_run())


# ---------------------------------------------------------------- 架构


def test_submit_has_no_command_literals():
    import inspect

    from helpcode.tui import app as app_mod

    src = inspect.getsource(app_mod.HelpCodeApp.submit)
    for literal in ('"/plan"', '"/do"', '"/compact"', '"/resume"', '"/exit"'):
        assert literal not in src


def test_command_modules_do_not_import_textual():
    import pathlib

    pkg = pathlib.Path("src/helpcode/command")
    for f in pkg.glob("*.py"):
        assert "textual" not in f.read_text(encoding="utf-8")


def test_command_pkg_does_not_import_tui():
    import pathlib

    pkg = pathlib.Path("src/helpcode/command")
    for f in pkg.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "helpcode.tui" not in text
        assert "from ..tui" not in text


def test_command_menu_selection_does_not_crash():
    """单 provider 下从命令菜单回车选中，不能崩（#provider_list 不存在）。

    Textual 8 会沿 MRO 同时调用 HelpCodeApp 与 SelectMixin 的同名处理器；
    后者若不判断列表 id，会把命令项当 provider，操作不存在的 #provider_list。
    """

    async def _run():
        app = _app()  # 单 provider → compose 不产出 #provider_list
        fp = FakeProvider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, fp)
            _input(app).text = "/"
            app.handle_tab()
            assert app.state == SessionState.COMPLETING
            # 直接投递 OptionSelected，确保走 Textual 的 MRO 分派链
            menu = app.query_one("#command_list", OptionList)
            menu.post_message(
                OptionList.OptionSelected(menu, menu.get_option_at_index(0), 0)
            )
            await pilot.pause()
            assert app.state == SessionState.IDLE
            assert _input(app).text.startswith("/")

    asyncio.run(_run())
