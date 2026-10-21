"""provider 选择界面（多 provider 时的 OptionList）。"""

from textual.widgets import OptionList, RichLog, Static

from ..llm import new_provider
from . import SessionState


class SelectMixin:
    """provider 选择逻辑，混入 HelpCodeApp。"""

    def _enter_selecting(self) -> None:
        """进入选择态：隐藏对话组件，仅显示选择列表。"""
        self.state = SessionState.SELECTING
        self.query_one("#log", RichLog).display = False
        self.query_one("#streaming", Static).display = False
        self.query_one("#input").display = False

    def _exit_selecting(self) -> None:
        """退出选择态：隐藏列表，显示对话组件，聚焦输入框。"""
        self.query_one("#provider_list", OptionList).display = False
        self.query_one("#log", RichLog).display = True
        self.query_one("#streaming", Static).display = True
        self.query_one("#input").display = True
        self.state = SessionState.IDLE
        self.query_one("#input").focus()

    def on_option_list_option_selected(self, event) -> None:
        """用户选定一个 provider 后进入对话。"""
        cfg = self.providers[event.option_index]
        self.provider = new_provider(cfg)
        self._update_statusbar()
        self._exit_selecting()
