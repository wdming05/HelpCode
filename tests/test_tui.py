"""TUI 权限交互单测（App.run_test + Pilot.press）。"""

import asyncio
import tempfile

from null_context import NullContext

from helpcode.agent import Agent
from helpcode.config import ProviderConfig
from helpcode.llm import StreamEvent, ToolCall
from helpcode.permission import Mode, new_engine
from helpcode.tool import Registry, ToolResult
from helpcode.tui import SessionState
from helpcode.tui.app import HelpCodeApp

_CFG = [ProviderConfig(name="qwen", protocol="openai", api_key="x", model="m")]


class FakeTool:
    name = "write_file"
    read_only = False
    description = "d"
    parameters = {"type": "object", "properties": {}}

    def run(self, **kw):
        return ToolResult(ok=True, content="written")


class FakeProvider:
    name = "fake"
    model = "m"

    def __init__(self, n_ask=1):
        self.n = 0
        self.n_ask = n_ask

    async def stream(self, req):
        self.n += 1
        if self.n <= self.n_ask:
            yield StreamEvent(
                done=True,
                tool_calls=[
                    ToolCall(
                        id=f"c{self.n}", name="write_file", input={"path": "x.txt"}
                    )
                ],
            )
        else:
            yield StreamEvent(text="完成")
            yield StreamEvent(done=True)


def _app_with_ask_provider():
    app = HelpCodeApp(_CFG)
    reg = Registry()
    reg.register(FakeTool())
    fp = FakeProvider()
    return app, reg, fp


def _setup(app, reg, fp):
    engine, _ = new_engine(tempfile.mkdtemp())  # 独立临时目录，避免持久化污染
    app.provider = fp
    app.agent = Agent(fp, reg, "dev", engine, NullContext())
    app.mode = Mode.DEFAULT


async def _wait_state(app, state, timeout=200):
    for _ in range(timeout):
        if app.state is state:
            return True
        await asyncio.sleep(0.01)
    return False


def test_shift_tab_cycle():
    async def _run():
        app = HelpCodeApp(_CFG)
        async with app.run_test() as pilot:
            await pilot.pause()
            seq = []
            for _ in range(4):
                await pilot.press("shift+tab")
                await pilot.pause()
                seq.append(str(app.mode))
            assert seq == ["acceptEdits", "plan", "bypassPermissions", "default"]

    asyncio.run(_run())


def test_approval_enter_allow():
    async def _run():
        app, reg, fp = _app_with_ask_provider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, reg, fp)
            await app.submit("写 x.txt")
            assert await _wait_state(app, SessionState.APPROVING)
            await pilot.press("enter")
            assert await _wait_state(app, SessionState.IDLE)
            lines = app.query_one("#log").lines
            assert any("write_file" in str(l) for l in lines)

    asyncio.run(_run())


def test_approval_number_keys():
    async def _run():
        app, reg, fp = _app_with_ask_provider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, reg, fp)
            await app.submit("写 x.txt")
            assert await _wait_state(app, SessionState.APPROVING)
            await pilot.press("2")  # 永久放行
            assert await _wait_state(app, SessionState.IDLE)

    asyncio.run(_run())


def test_approval_deny():
    async def _run():
        app, reg, fp = _app_with_ask_provider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, reg, fp)
            await app.submit("写 x.txt")
            assert await _wait_state(app, SessionState.APPROVING)
            await pilot.press("n")  # 拒绝本次
            assert await _wait_state(app, SessionState.IDLE)

    asyncio.run(_run())


def test_esc_cancel_no_leak():
    async def _run():
        app, reg, fp = _app_with_ask_provider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, reg, fp)
            await app.submit("写 x.txt")
            assert await _wait_state(app, SessionState.APPROVING)
            await pilot.press("escape")
            assert await _wait_state(app, SessionState.IDLE)
            # Future 已 resolve、pending 清空，历史合法
            assert app._pending_request is None

    asyncio.run(_run())


def test_mode_persists_across_turns():
    async def _run():
        app, reg, fp = _app_with_ask_provider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, reg, fp)
            await pilot.press("shift+tab")  # acceptEdits
            await pilot.pause()
            assert app.mode is Mode.ACCEPT_EDITS
            await app.submit("写 x.txt")
            assert await _wait_state(app, SessionState.IDLE)
            assert app.mode is Mode.ACCEPT_EDITS  # 跨轮保持

    asyncio.run(_run())


# ---------------------------------------------------------------- ch08：命令回归


def test_unknown_slash_command_does_not_call_model():
    """ch10：未知斜杠命令不送模型，只提示 /help（N5/AC5）。"""

    async def _run():
        from textual.widgets import RichLog

        app, reg, fp = _app_with_ask_provider()
        fp = FakeProvider(n_ask=0)
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, reg, fp)
            log = app.query_one("#log", RichLog)
            before = len(log.lines)

            await app.submit("/unknown_cmd")
            await pilot.pause()

            assert fp.n == 0  # 没发给模型
            assert len(log.lines) > before
            assert any("未知命令" in str(line) for line in log.lines)

    asyncio.run(_run())


def test_compact_command_does_not_call_model():
    """/compact 走本地压缩，不进 STREAMING、不发请求。"""

    async def _run():
        from textual.widgets import RichLog

        app, reg, fp = _app_with_ask_provider()
        fp = FakeProvider(n_ask=0)
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, reg, fp)
            log = app.query_one("#log", RichLog)
            before = len(log.lines)

            await app.submit("/compact")
            await pilot.pause()

            assert app.state is SessionState.IDLE
            assert fp.n == 0
            assert len(log.lines) > before  # 写了「无需压缩」

    asyncio.run(_run())


def test_exit_command_quits_app():
    """/exit 正常退出，不留挂起任务。"""

    async def _run():
        app, reg, fp = _app_with_ask_provider()
        async with app.run_test() as pilot:
            await pilot.pause()
            _setup(app, reg, fp)
            await app.submit("/exit")
            await pilot.pause()
            assert app.return_code is not None

    asyncio.run(_run())


# ---------------------------------------------------------------- ch09：会话渲染


def _meta(title="标题", model="qwen", size=2048, messages=4, updated=1000.0):
    from pathlib import Path

    from helpcode.memory.session import SessionMeta

    return SessionMeta(
        session_id="20261024-120000-ab12",
        dir=Path("/tmp/x"),
        jsonl=Path("/tmp/x/conversation.jsonl"),
        title=title,
        messages=messages,
        model=model,
        updated=updated,
        size_bytes=size,
    )


def test_ago_formats():
    """四档相对时间：分钟 / 小时 / 昨天 / N 天前。"""
    from helpcode.tui.view import _ago

    assert _ago(30) == "刚刚"
    assert _ago(120) == "2 分钟前"
    assert _ago(7200) == "2 小时前"
    assert _ago(86400 + 10) == "昨天"
    assert _ago(86400 * 5) == "5 天前"


def test_session_option_has_four_fields():
    """列表项包含标题 / 相对时间 / 模型 / 大小（AC37）。"""
    from helpcode.tui.view import render_session_option

    text = render_session_option(_meta(), now=1000.0 + 7200).plain

    assert "标题" in text
    assert "小时前" in text
    assert "qwen" in text
    assert "2.0 KB" in text
    assert "4 条" in text


def test_repaired_empty_when_clean():
    """没有修复时返回空 Text——调用方据此不写那一行（AC34）。"""
    from helpcode.tui.view import render_cleanup, render_repaired

    assert render_repaired("").plain == ""
    assert render_cleanup("").plain == ""


def test_restored_line_contains_count():
    from helpcode.tui.view import render_restored

    line = render_restored("20261024-120000-ab12", 7).plain
    assert "已恢复会话" in line
    assert "7 条消息" in line


def test_submit_waits_restore_compress():
    """AC16：恢复后的压缩没跑完，第一轮请求不能发出去。

    压缩是后台 task（要调 LLM，不能阻塞 on_mount），但 `submit` 必须在
    发起请求前 await 它——否则用户手快一点，请求带的还是压缩前的历史，
    该撞的 400 照样撞。
    """
    order = []

    class _Prov:
        name = "fake"
        model = "m"

        async def stream(self, req):
            order.append("request")
            yield StreamEvent(text="ok")
            yield StreamEvent(done=True)

    async def _run():
        app = HelpCodeApp(_CFG)
        async with app.run_test() as pilot:
            await pilot.pause()
            prov = _Prov()
            engine, _ = new_engine(tempfile.mkdtemp())
            app.provider = prov
            app.agent = Agent(prov, Registry(), "dev", engine, NullContext())
            app.mode = Mode.DEFAULT
            app.state = SessionState.IDLE

            async def _slow():
                order.append("compress-start")
                await asyncio.sleep(0.05)
                order.append("compress-done")

            app._restore_compress_task = asyncio.create_task(_slow())
            await app.submit("你好")

            for _ in range(100):
                if "request" in order:
                    break
                await asyncio.sleep(0.01)

            assert order.index("compress-done") < order.index("request")
            assert app._restore_compress_task is None  # 只等一次

    asyncio.run(_run())


def test_index_snapshot_not_reloaded_mid_session(tmp_path):
    """N1：记忆是会话开始时的快照，改磁盘上的文件不影响本会话。"""
    mem = tmp_path / "index.md"
    mem.write_text("- 旧条目", encoding="utf-8")

    app = HelpCodeApp(_CFG, instructions="规则", memory=mem.read_text(encoding="utf-8"))
    # 模拟本轮笔记写入了新条目
    mem.write_text("- 新条目", encoding="utf-8")

    app.provider = FakeProvider()
    app._setup_agent()

    assert app.agent._system_extra == ("规则", "- 旧条目")


def test_note_hook_failure_degrades(monkeypatch):
    """AC30：笔记写盘失败 → 只打一行提示，不影响本轮对话。"""
    from helpcode.conversation import Conversation

    async def boom(*args, **kwargs):
        raise OSError("disk full")

    async def _run():
        app = HelpCodeApp(_CFG)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.provider = FakeProvider()
            monkeypatch.setattr("helpcode.tui.app.notes.update", boom)

            await app._note_hook(Conversation())  # 不抛

            lines = [str(line) for line in app.query_one("#log").lines]
            assert any("笔记更新跳过" in line for line in lines)

    asyncio.run(_run())
