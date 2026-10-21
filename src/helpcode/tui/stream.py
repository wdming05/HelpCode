"""流式消费与计时逻辑（作为 HelpCodeApp 的 mixin）。"""

import asyncio
import time

from textual.widgets import RichLog, Static

from . import SessionState
from .view import error_block, render_markdown, render_tool_line


class StreamMixin:
    """流式接入与计时的方法集合，混入 HelpCodeApp。"""

    async def _consume_stream(self, text: str) -> None:
        """消费 Agent 事件：文本流式 + 工具行 + 收尾。"""
        try:
            async for ev in self.agent.run(text):
                if ev.err is not None:
                    self._finish_with_error(ev.err)
                    return
                if ev.text:
                    self.cur_reply += ev.text
                    self._refresh_streaming_view()
                if ev.tool_call is not None and ev.tool_result is not None:
                    log = self.query_one("#log", RichLog)
                    log.write(render_tool_line(ev.tool_call, ev.tool_result))
                if ev.done:
                    self._finish_with_assistant(self.cur_reply)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._finish_with_error(e)

    def _tick(self) -> None:
        """计时刷新（0.1s 一次），仅流式期间更新。"""
        if self.state != SessionState.STREAMING:
            return
        elapsed = int(time.monotonic() - self.turn_start)
        self.query_one("#streaming", Static).update(
            f"● {self.cur_reply}\n\nImagining… ({elapsed}s)"
        )

    def _refresh_streaming_view(self) -> None:
        """流式期间实时显示已收文本。"""
        self.query_one("#streaming", Static).update(f"● {self.cur_reply}")

    def _finish_with_assistant(self, reply: str) -> None:
        """done：markdown 定型追加到 RichLog，记录耗时，回 IDLE。"""
        elapsed = time.monotonic() - self.turn_start
        log = self.query_one("#log", RichLog)
        log.write(render_markdown(reply))
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
        self.query_one("#streaming", Static).update("")
        self.state = SessionState.IDLE
