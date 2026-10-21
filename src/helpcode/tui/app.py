"""TUI App：HelpCodeApp、状态机与布局。"""

import asyncio
import os
import time

from textual.app import App, ComposeResult
from textual.widgets import OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from .. import __version__
from ..config import ProviderConfig
from ..conversation import Conversation
from ..llm import Provider, new_provider
from ..prompt import render_banner
from . import SessionState
from .select import SelectMixin
from .stream import StreamMixin
from .view import render_statusbar, user_block


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
            event.stop()#停止事件冒泡，不要让父组件也处理这个按键。
            event.prevent_default()#阻止默认行为，即不让 TextArea 在光标处插入换行。
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

    BINDINGS = [("ctrl+c", "quit", "退出")]

    def __init__(self, providers: list[ProviderConfig]) -> None:
        super().__init__()
        self.providers = providers
        self.state = SessionState.SELECTING
        self.provider: Provider | None = None
        self.conv = Conversation()
        self.cur_reply = ""
        self.turn_start = 0.0
        self._stream_task = None
        self._timer = None

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
            self.state = SessionState.IDLE
            self._update_statusbar()
        else:
            self.query_one("#statusbar", Static).update(
                "请选择一个 provider（↑↓ 移动，Enter 选定）"
            )
            self._enter_selecting()

    def _update_statusbar(self) -> None:
        """更新底部状态栏：左 provider 名，右模型名。"""
        if self.provider is None:
            return
        self.query_one("#statusbar", Static).update(
            render_statusbar(self.provider.name, self.provider.model)
        )

    async def submit(self, text: str) -> None:
        """处理一次输入提交。"""
        if self.state != SessionState.IDLE:
            return  # 流式/选择期间不接受新的提交
        text = text.strip()
        if not text:
            return
        if text == "/exit":
            await self.action_quit()
            return

        self.conv.add_user(text)
        log = self.query_one("#log", RichLog)
        log.write(user_block(text))
        self.query_one("#input", InputArea).clear()
        self.cur_reply = ""
        self.turn_start = time.monotonic()
        self.state = SessionState.STREAMING
        self._stream_task = asyncio.create_task(self._consume_stream())
        self._timer = self.set_interval(0.1, self._tick)

    async def action_quit(self) -> None:
        """安全退出：取消进行中的流，恢复终端。"""
        if self._stream_task is not None:
            self._stream_task.cancel()
        self.exit()
