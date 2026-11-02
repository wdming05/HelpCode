"""会话恢复界面：列表、过滤、切换、启动提示。"""

import asyncio
import json
import tempfile
from pathlib import Path

from null_context import NullContext

from helpcode.agent import Agent
from helpcode.config import ProviderConfig
from helpcode.llm import StreamEvent
from helpcode.memory import session as ses
from helpcode.memory.restore import RestoreReport
from helpcode.permission import Mode, new_engine
from helpcode.tool import Registry
from helpcode.tui import SessionState
from helpcode.tui.app import HelpCodeApp

_CFG = [ProviderConfig(name="qwen", protocol="openai", api_key="x", model="m")]


class _Provider:
    name = "fake"
    model = "m"

    async def stream(self, req):
        yield StreamEvent(text="ok")
        yield StreamEvent(done=True)


def _make_sessions(root: Path, specs: list[tuple[str, str, str]]) -> list:
    """造若干会话，返回扫描结果。"""
    for sid, title, model in specs:
        ses.ensure_session_dir(root, sid)
        lines = [
            {"role": "user", "content": title, "ts": "x", "model": model},
            {"role": "assistant", "content": "回答", "ts": "x", "model": model},
        ]
        (root / ".helpcode" / "sessions" / sid / "conversation.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines),
            encoding="utf-8",
        )
    return ses.scan(root)


def _app(tmp_path: Path, sessions: list, **kw) -> HelpCodeApp:
    app = HelpCodeApp(_CFG, sessions=sessions, **kw)
    app._cwd = tmp_path  # 测试里不依赖 os.getcwd()
    return app


def _setup(app):
    app.provider = _Provider()
    engine, _ = new_engine(tempfile.mkdtemp())
    app.agent = Agent(_Provider(), Registry(), "dev", engine, NullContext())
    app.mode = Mode.DEFAULT
    app.state = SessionState.IDLE


def _run(coro_factory):
    return asyncio.run(coro_factory())


def test_resume_lists_all_sessions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sessions = _make_sessions(
        tmp_path,
        [
            ("20261020-100000-aaaa", "第一个会话", "m1"),
            ("20261021-100000-bbbb", "第二个会话", "m2"),
            ("20261022-100000-cccc", "第三个会话", "m3"),
        ],
    )

    async def _go():
        app = _app(tmp_path, sessions)
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app)
            app._enter_resume()
            await pilot.pause()
            assert app.state is SessionState.RESUMING
            assert len(app._resume_items) == 3

    _run(_go)


def test_filter_narrows_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sessions = _make_sessions(
        tmp_path,
        [
            ("20261020-100000-aaaa", "内存泄漏排查", "m1"),
            ("20261021-100000-bbbb", "打包脚本", "m2"),
        ],
    )

    async def _go():
        app = _app(tmp_path, sessions)
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app)
            app._enter_resume()
            app._rebuild_resume_list("泄漏")
            await pilot.pause()
            assert [m.title for m in app._resume_items] == ["内存泄漏排查"]

    _run(_go)


def test_filter_no_match_shows_hint(tmp_path, monkeypatch):
    """无匹配时列表里有一条 disabled 提示，不是空列表（AC37）。"""
    monkeypatch.chdir(tmp_path)
    sessions = _make_sessions(tmp_path, [("20261020-100000-aaaa", "会话", "m")])

    async def _go():
        from textual.widgets import OptionList

        app = _app(tmp_path, sessions)
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app)
            app._enter_resume()
            app._rebuild_resume_list("绝对匹配不到")
            await pilot.pause()

            listing = app.query_one("#resume_list", OptionList)
            assert listing.option_count == 1
            assert app._resume_items == []

    _run(_go)


def test_escape_returns_to_idle(tmp_path, monkeypatch):
    """Esc 退出选会话，当前会话不变（N4）。"""
    monkeypatch.chdir(tmp_path)
    sessions = _make_sessions(tmp_path, [("20261020-100000-aaaa", "会话", "m")])

    async def _go():
        app = _app(tmp_path, sessions)
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app)
            before = app.conv
            app._enter_resume()
            await pilot.pause()
            await app.action_cancel()
            await pilot.pause()
            assert app.state is SessionState.IDLE
            assert app.conv is before

    _run(_go)


def test_banner_shows_restored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = RestoreReport(messages=[])

    async def _go():
        from textual.widgets import RichLog

        app = _app(tmp_path, [], restore=report, session_id="20261020-100000-aaaa")
        async with app.run_test() as pilot:
            await pilot.pause()
            text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "已恢复会话" in text

    _run(_go)


def test_no_restored_line_when_new_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def _go():
        from textual.widgets import RichLog

        app = _app(tmp_path, [])
        async with app.run_test() as pilot:
            await pilot.pause()
            text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "已恢复会话" not in text

    _run(_go)


def test_resume_switches_conv_and_writer(tmp_path, monkeypatch):
    """切换会话后：历史换成目标会话的、writer 指向目标文件（AC19）。"""
    monkeypatch.chdir(tmp_path)
    sessions = _make_sessions(
        tmp_path,
        [
            ("20261020-100000-aaaa", "旧会话", "m1"),
            ("20261021-100000-bbbb", "要切过去的会话", "m2"),
        ],
    )

    async def _go():
        app = _app(tmp_path, sessions)
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app)
            app.session_id = sessions[0].session_id if sessions else None
            target = next(s for s in sessions if s.title == "要切过去的会话")

            app._apply_resume(target)
            await pilot.pause()

            assert app.session_id == target.session_id
            assert app.conv.messages()[0].content == "要切过去的会话"
            assert app.writer.jsonl == target.jsonl

    _run(_go)


def test_quit_drains_note_tasks(tmp_path, monkeypatch):
    """退出前等待沉淀任务，不留挂起 task（N7）。"""
    monkeypatch.chdir(tmp_path)
    finished = []

    async def _go():
        app = _app(tmp_path, [])
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app)

            async def slow_hook(conv):
                await asyncio.sleep(0.05)
                finished.append(True)

            app.agent._on_turn_end = slow_hook
            app.agent._maybe_start_note_task(app.conv)
            await app.agent.drain_note_tasks()

            assert finished == [True]

    _run(_go)
