"""TUI App：HelpCodeApp、状态机与布局。"""

import asyncio
import os
import time

from textual.app import App, ComposeResult
from textual.widgets import OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from .. import __version__
from ..agent import Agent
from ..config import ProviderConfig
from ..conversation import Conversation
from ..llm import Provider, new_provider
from ..permission import Mode, Outcome, new_engine
from ..prompt import EXECUTE_DIRECTIVE, render_banner
from ..tool import new_default_registry
from . import SessionState
from .select import SelectMixin
from .stream import StreamMixin
from .view import render_permission_request, render_statusbar, user_block


class InputArea(TextArea):
    """输入框：Enter 提交，Alt+Enter 换行。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "❯"
        self.border_subtitle = "Send a message..."

    async def _on_key(self, event) -> None:
        """拦截 Enter（提交）与 Alt+Enter（换行），其余交给 TextArea 默认处理。"""
        key = event.key
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.action_submit_input()
            return
        if key == "alt+enter":
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self._replace_via_keyboard("\n", start, end)
            return
        await super()._on_key(event)

    def action_submit_input(self) -> None:
        """Enter 提交：把文本交给 App 处理。"""
        app = self.app
        if isinstance(app, HelpCodeApp):
            asyncio.create_task(app.submit(self.text))


class HelpCodeApp(StreamMixin, SelectMixin, App):
    """HelpCode 终端界面。"""

    CSS = """
    #log {
        height: 1fr;
        border: none;
    }
    #streaming {
        height: auto;
        min-height: 0;
        padding: 0 1;
    }
    #input {
        height: auto;
        border: solid $primary;
    }
    #statusbar {
        height: 1;
        padding: 0 1;
    }
    #provider_list {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "退出"),
        ("escape", "cancel", "取消"),
    ]

    def __init__(
        self, providers: list[ProviderConfig], mcp_tools: list | None = None
    ) -> None:
        super().__init__()
        self.providers = providers
        self.mcp_tools = mcp_tools or []
        self.state = SessionState.SELECTING
        self.provider: Provider | None = None
        self.agent: Agent | None = None
        self.engine = None
        self.conv = Conversation()
        self.cancel_event = asyncio.Event()
        self.mode = Mode.DEFAULT
        self.cur_reply = ""
        self._cur_iteration = 0
        self.turn_start = 0.0
        self._stream_task = None
        self._timer = None
        self._pending_request = None
        self._approve_cursor = 0

    def compose(self) -> ComposeResult:
        yield RichLog(id="log", wrap=True, markup=False)
        yield Static("", id="streaming")
        yield InputArea(id="input")
        yield Static("", id="statusbar")
        if len(self.providers) > 1:
            yield OptionList(
                *[Option(f"{p.name} ({p.model})") for p in self.providers],
                id="provider_list",
            )

    def on_mount(self) -> None:
        """启动：写 banner；单 provider 直进 IDLE，多 provider 进 SELECTING。"""
        log = self.query_one("#log", RichLog)
        log.write(render_banner(__version__, os.getcwd()))

        if len(self.providers) == 1:
            self.provider = new_provider(self.providers[0])
            self._setup_agent()
            self.state = SessionState.IDLE
            self._update_statusbar()
        else:
            self.query_one("#statusbar", Static).update(
                "请选择一个 provider（↑↓ 移动，Enter 选定）"
            )
            self._enter_selecting()

    def _update_statusbar(self) -> None:
        """更新底部状态栏：左权限模式名、右模型名。"""
        if self.provider is None:
            return
        self.query_one("#statusbar", Static).update(
            render_statusbar(self.mode, self.provider.model)
        )

    def _setup_agent(self) -> None:
        """provider 确定后，构造权限引擎与 Agent。

        注册顺序固定：内置 6 个在前，MCP 工具在后（已按 server/name 排序）。
        tools 段位于 system 之前，顺序一变 prompt 缓存前缀就全失效。
        """
        self.engine, _ = new_engine(os.getcwd())
        registry = new_default_registry()
        for tool in self.mcp_tools:
            registry.register(tool)
        self.agent = Agent(self.provider, registry, __version__, self.engine)

    async def submit(self, text: str) -> None:
        """处理一次输入提交。"""
        if self.state != SessionState.IDLE:
            return  # 流式/待批准/选择期间不接受新的提交
        text = text.strip()
        if not text:
            return
        if text == "/exit":
            await self.action_quit()
            return
        if text == "/plan":
            self.mode = Mode.PLAN
            self._update_statusbar()
            log = self.query_one("#log", RichLog)
            log.write(user_block("/plan"))
            log.write("已进入计划模式（仅只读工具，可 /do 切换回执行）")
            return
        if text == "/do":
            self.mode = Mode.DEFAULT
            self._update_statusbar()
            text = EXECUTE_DIRECTIVE  # 注入执行指令，触发模型按计划执行

        self.cancel_event = asyncio.Event()  # 每次提交重置取消信号
        log = self.query_one("#log", RichLog)
        log.write(user_block(text))
        self.query_one("#input", InputArea).clear()
        self.cur_reply = ""
        self._cur_iteration = 0
        self.turn_start = time.monotonic()
        self.state = SessionState.STREAMING
        self._stream_task = asyncio.create_task(self._consume_stream(text))
        self._timer = self.set_interval(0.1, self._tick)

    def action_cycle_mode(self) -> None:
        """Shift+Tab：循环切换权限模式（仅 IDLE 生效）。"""
        if self.state != SessionState.IDLE:
            return
        self.mode = Mode((int(self.mode) + 1) % 4)
        self._update_statusbar()
        log = self.query_one("#log", RichLog)
        log.write(user_block(f"已切换到权限模式: {self.mode}"))

    def on_key(self, event) -> None:
        """APPROVING 态拦截选择按键；IDLE 态 shift+tab 切换模式。"""
        if self.state == SessionState.APPROVING:
            key = event.key
            if key in ("up", "k"):
                self._approve_cursor = (self._approve_cursor - 1) % 3
                event.stop()
                self._refresh_approval()
            elif key in ("down", "j"):
                self._approve_cursor = (self._approve_cursor + 1) % 3
                event.stop()
                self._refresh_approval()
            elif key == "enter":
                event.stop()
                self._resolve_approval(self._outcome_for_index(self._approve_cursor))
            elif key in ("1", "2", "3"):
                event.stop()
                self._resolve_approval(self._outcome_for_index(int(key) - 1))
            elif key == "y":
                event.stop()
                self._resolve_approval(Outcome.ALLOW_ONCE)
            elif key in ("n", "d"):
                event.stop()
                self._resolve_approval(Outcome.DENY_ONCE)
            return
        if event.key == "shift+tab":
            event.stop()
            event.prevent_default()
            self.action_cycle_mode()

    def _outcome_for_index(self, idx: int) -> Outcome:
        return [Outcome.ALLOW_ONCE, Outcome.ALLOW_FOREVER, Outcome.DENY_ONCE][idx]

    def _refresh_approval(self) -> None:
        if self._pending_request is not None:
            self.query_one("#streaming", Static).update(
                render_permission_request(self._pending_request, self._approve_cursor)
            )

    def _resolve_approval(self, outcome: Outcome) -> None:
        """回填用户决策，恢复 STREAMING 让 agent 继续。"""
        if (
            self._pending_request is not None
            and not self._pending_request.respond.done()
        ):
            self._pending_request.respond.set_result(outcome)
        self._pending_request = None
        self.query_one("#streaming", Static).update("")
        self.state = SessionState.STREAMING
        self.query_one("#input", InputArea).focus()

    async def action_cancel(self) -> None:
        """Esc：流式/待批准态取消当前循环（协作式，不退出程序）。"""
        if self.state == SessionState.APPROVING:
            # 先 resolve future 解开 agent 等待，再置取消信号
            if (
                self._pending_request is not None
                and not self._pending_request.respond.done()
            ):
                self._pending_request.respond.set_result(Outcome.DENY_ONCE)
            self._pending_request = None
            self.query_one("#streaming", Static).update("")
            self.state = SessionState.STREAMING
            self.cancel_event.set()
        elif self.state == SessionState.STREAMING:
            self.cancel_event.set()

    async def action_quit(self) -> None:
        """Ctrl+C：流式/待批准态取消、空闲态退出。"""
        if self.state in (SessionState.STREAMING, SessionState.APPROVING):
            await self.action_cancel()
        else:
            self.exit()
