"""会话恢复界面：列表 + 即时过滤。

交互形态选的是**弹窗列表 + 输入框即时过滤**，不是 `/resume 2` 这种带参数的命令。
理由是 ``/resume git`` 做不到「边打字边缩小范围」——而会话列表通常会攒到几十个，
一次只看到前十个的话，用户得先报编号再看结果，来回两三次。

按钮路由按 ``OptionList`` 的 id 分派：provider 选择器和会话列表共用
``on_option_list_option_selected`` 这一个事件。
"""

from __future__ import annotations

import time

from textual.widgets import Input, OptionList, RichLog
from textual.widgets.option_list import Option

from . import SessionState
from .view import render_session_option


class ResumeMixin:
    """会话列表逻辑，混入 HelpCodeApp。"""

    def _enter_resume(self) -> None:
        """进入选会话态：显示过滤框与列表。"""
        if self.state != SessionState.IDLE:
            return
        self.state = SessionState.RESUMING
        self.query_one("#log", RichLog).write("选择要恢复的会话（Esc 取消）")
        self.query_one("#resume_filter", Input).display = True
        self.query_one("#resume_list", OptionList).display = True
        self.query_one("#input").display = False
        self._rebuild_resume_list("")
        self.query_one("#resume_filter", Input).focus()

    def _exit_resume(self) -> None:
        """退出选会话态（不改变当前会话）。"""
        self.query_one("#resume_filter", Input).display = False
        self.query_one("#resume_list", OptionList).display = False
        self.query_one("#input").display = True
        self.state = SessionState.IDLE
        self.query_one("#input").focus()

    def _rebuild_resume_list(self, keyword: str = "") -> None:
        """按关键字过滤并重建列表。

        无匹配时放一条 disabled 的提示项，而不是空列表——空列表看起来像
        「加载失败」，用户不知道是没匹配还是坏了。
        """
        listing = self.query_one("#resume_list", OptionList)
        listing.clear_options()

        needle = keyword.strip().lower()
        items = [s for s in self.sessions if needle in (s.title or "").lower()]
        self._resume_items = items

        if not items:
            listing.add_option(Option("没有匹配的会话", disabled=True))
            return

        now = time.time()
        for meta in items:
            listing.add_option(
                Option(render_session_option(meta, now), id=meta.session_id)
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        """过滤框内容变化 → 即时重建列表。"""
        if event.input.id != "resume_filter":
            return
        if self.state != SessionState.RESUMING:
            return
        self._rebuild_resume_list(event.value)

    def _select_session(self, index: int) -> None:
        """选定第 index 项：切会话、切存档、切落盘目录。"""
        items = getattr(self, "_resume_items", [])
        if not (0 <= index < len(items)):
            return
        self._apply_resume(items[index])
        self._exit_resume()
