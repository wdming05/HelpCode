"""流式消费与计时逻辑（作为 HelpCodeApp 的 mixin）。"""

import asyncio
import time

from textual.widgets import RichLog, Static

from . import SessionState
from .view import (
    error_block,
    render_markdown,
    render_notice,
    render_permission_request,
    render_token_usage,
    render_tool_end,
    render_tool_start,
)


class StreamMixin:
    """流式接入与计时的方法集合，混入 HelpCodeApp。"""

    async def _consume_stream(self, text: str) -> None:
        """消费 Agent 事件：文本流式 + 工具行 + 轮次 + 收尾。"""
        usage = None
        try:
            async for ev in self.agent.run(
                text, self.conv, self.cancel_event, self.mode
            ):
                if ev.err is not None:
                    self._finish_with_error(ev.err)
                    return
                if ev.iteration is not None:
                    self._cur_iteration = ev.iteration
                if ev.text:
                    self.cur_reply += ev.text
                    self._refresh_streaming_view()
                if ev.tool_start is not None:
                    log = self.query_one("#log", RichLog)
                    log.write(render_tool_start(ev.tool_start))
                if ev.tool_end is not None:
                    log = self.query_one("#log", RichLog)
                    log.write(render_tool_end(ev.tool_call, ev.tool_end))
                if ev.token_usage is not None:
                    usage = ev.token_usage
                if ev.permission_request is not None:
                    self._pending_request = ev.permission_request
                    self._approve_cursor = 0
                    self.state = SessionState.APPROVING
                    self.set_focus(None)  # 移走焦点，让 App 级 on_key 处理选择按键
                    self.query_one("#streaming", Static).update(
                        render_permission_request(ev.permission_request, 0)
                    )
                if ev.notice is not None:
                    log = self.query_one("#log", RichLog)
                    log.write(render_notice(ev.notice))
                if ev.done:
                    self._finish_with_assistant(self.cur_reply, usage)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._finish_with_error(e)

    def _tick(self) -> None:
        """计时刷新（0.1s 一次），仅流式期间更新；含迭代轮次。"""
        if self.state != SessionState.STREAMING:
            return
        elapsed = int(time.monotonic() - self.turn_start)
        iteration = getattr(self, "_cur_iteration", 0)
        suffix = f" · 第 {iteration}/25 轮" if iteration else ""
        self.query_one("#streaming", Static).update(
            f"● {self.cur_reply}\n\nImagining… ({elapsed}s){suffix}"
        )

    def _refresh_streaming_view(self) -> None:
        """流式期间实时显示已收文本。"""
        self.query_one("#streaming", Static).update(f"● {self.cur_reply}")

    def _finish_with_assistant(self, reply: str, usage=None) -> None:
        """done：markdown 定型 + Token 用量入 RichLog，回 IDLE。"""
        elapsed = time.monotonic() - self.turn_start
        log = self.query_one("#log", RichLog)
        log.write(render_markdown(reply))
        if usage is not None:
            log.write(render_token_usage(usage))
        log.write(f"（耗时 {elapsed:.1f}s）")
        self._stop_stream()

    def _finish_with_error(self, err: Exception) -> None:
        """err：错误块追加，回 IDLE（不退出）。"""
        log = self.query_one("#log", RichLog)
        log.write(error_block(err))
        self._stop_stream()

    def _stop_stream(self) -> None:
        """清理流式状态，回到空闲。"""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._stream_task = None
        self.cur_reply = ""
        self._cur_iteration = 0
        self.query_one("#streaming", Static).update("")
        self.state = SessionState.IDLE
