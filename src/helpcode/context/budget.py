"""编排层：什么时候压、压到哪、失败了怎么办。

三层判断顺序不能反：
1. 第一层（落盘）——不调 LLM，永不失败，先做；
2. 总量估算——用锚点 + 增量，偏保守；
3. 第二层（摘要）——调 LLM，可能失败，有熔断。
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..conversation import Conversation
from ..llm import Message
from . import resume, spill
from .constants import (
    KEEP_TAIL_MIN_MSGS,
    MIN_SUMMARIZE_MSGS,
    MIN_WINDOW_RATIO,
    SAFETY_AUTO,
    SAFETY_MANUAL,
    SUMMARY_MAX_FAILURES,
)
from .estimate import Anchor, message_tokens
from .summarize import CompressCancelled, find_cut, next_cut, summarize

logger = logging.getLogger(__name__)

# 一次压缩最多重试几轮（F9 降级：压完仍超线时扩大摘要范围）
MAX_DEGRADE_ATTEMPTS = 2


@dataclass
class CompressInfo:
    """压缩事件：给了 token 数字，用户才能判断这次压缩值不值。"""

    phase: str  # "start" | "done"
    tokens_before: int = 0
    tokens_after: int = 0
    summarized: int = 0
    kept: int = 0
    spilled: int = 0


@dataclass
class CompressReport:
    spilled: int = 0
    summarized: int = 0
    kept: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    notice: str = ""
    cancelled: bool = False
    failed: str = ""
    emergency: bool = False
    info: CompressInfo | None = field(default=None)


class ContextManager:
    """两层的编排者：持有锚点、熔断状态与窗口配置。"""

    def __init__(
        self,
        provider,
        root: Path,
        context_window: int,
        spill_subdir: Path | None = None,
    ) -> None:
        self.provider = provider
        self.root = Path(root)
        self._spill_subdir = spill_subdir
        self.context_window = context_window
        self.anchor: Anchor | None = None
        self.consecutive_failures = 0
        self.fuse_blown = False
        self._notified_disabled = False

        self.auto_enabled = context_window >= SAFETY_AUTO * MIN_WINDOW_RATIO
        if not self.auto_enabled:
            logger.warning(
                "context_window=%d 过小（< %.0f），自动压缩已关闭",
                context_window,
                SAFETY_AUTO * MIN_WINDOW_RATIO,
            )

    # ------------------------------------------------------------ 估算

    def estimate(self, conv: Conversation) -> int:
        """当前上下文的估算 token 数（锚点 + 增量）。"""
        return self._estimate_total(conv.messages())

    def _estimate_total(self, messages: list[Message]) -> int:
        if self.anchor is None or self.anchor.message_count > len(messages):
            return sum(message_tokens(m) for m in messages)
        return self.anchor.total_tokens + sum(
            message_tokens(m) for m in messages[self.anchor.message_count :]
        )

    def after_stream(self, usage, message_count: int) -> None:
        """用服务端实测值重建锚点。

        ★ 必须把缓存部分加回来：Anthropic 的 ``input_tokens`` 不含
        ``cache_read``，只取它会把上下文长度低估一个数量级，
        导致压缩永远不触发，最后被 400 打回来。
        """
        self.anchor = Anchor(
            total_tokens=usage.input_tokens + usage.cache_read + usage.cache_write,
            message_count=message_count,
        )

    def reset_anchor(self) -> None:
        self.anchor = None

    # ------------------------------------------------------------ 判断

    def should_compress(self, conv: Conversation) -> bool:
        """纯估算，不碰 LLM；供 agent 在压缩前先发一个 start 事件。"""
        if not self.auto_enabled:
            return False
        return self.estimate(conv) >= self.context_window - SAFETY_AUTO

    def has_room_after_compress(self, tokens_after: int) -> bool:
        """紧急压缩后是否还值得重试：贴着窗口就说明压不动，重试必然再撞。"""
        return tokens_after < self.context_window - SAFETY_MANUAL

    # ------------------------------------------------------------ 入口

    async def before_request(
        self, conv: Conversation, cancel_event, tool_names: list[str]
    ) -> CompressReport:
        """每次 API 请求之前：先落盘，再按需摘要。"""
        # 扫描 conv 里的工具结果，超过阈值的写到磁盘，对话里只留预览和路径。
        spilled, warnings = spill.apply(conv, self.root, self._spill_subdir)
       
        if spilled:
            self.reset_anchor()

        total = self.estimate(conv)
        report = CompressReport(spilled=spilled, tokens_before=total)

        for w in warnings:
            logger.warning(w)

        result = await self._decide(conv, cancel_event, tool_names, report)
        if spilled and not result.notice:
            result.notice = f"{spilled} 条工具结果已落盘（对话中只留预览）"
        return result

    async def _decide(
        self,
        conv: Conversation,
        cancel_event,
        tool_names: list[str],
        report: CompressReport,
    ) -> CompressReport:
        total = report.tokens_before

        if not self.auto_enabled:
            if not self._notified_disabled:
                self._notified_disabled = True
                report.notice = (
                    f"上下文窗口配置过小（{self.context_window}），自动压缩已关闭；"
                    "可用 /compact 手动触发"
                )
            return report

        if total < self.context_window - SAFETY_AUTO:
            return report

        if self.fuse_blown:
            report.notice = (
                f"自动压缩已熔断（连续失败 {self.consecutive_failures} 次），"
                "可用 /compact 手动重试"
            )
            return report

        messages = conv.messages()
        if len(messages) < KEEP_TAIL_MIN_MSGS + MIN_SUMMARIZE_MSGS:
            report.notice = "历史过短，跳过压缩"
            return report

        return await self._compress(
            conv, messages, total, cancel_event, tool_names, report
        )

    async def compact_now(
        self,
        conv: Conversation,
        emergency: bool = False,
        tool_names: list[str] | None = None,
    ) -> CompressReport:
        """手动或紧急压缩：不受阈值、熔断、窗口下界限制。"""
        messages = conv.messages()
        total = self._estimate_total(messages)
        report = CompressReport(tokens_before=total, emergency=emergency)

        if not messages:
            report.notice = "无需压缩（历史为空）"
            return report
        if len(messages) < KEEP_TAIL_MIN_MSGS + MIN_SUMMARIZE_MSGS:
            report.notice = "无需压缩（历史过短）"
            return report

        note = ""
        if self.fuse_blown and not emergency:
            note = f"（自动压缩此前连续失败 {self.consecutive_failures} 次）"

        report = await self._compress(
            conv, messages, total, None, list(tool_names or []), report
        )
        if note:
            report.notice = note + report.notice
        return report

    # ------------------------------------------------------------ 核心

    async def _compress(
        self,
        conv: Conversation,
        messages: list[Message],
        total: int,
        cancel_event,
        tool_names: list[str],
        report: CompressReport,
    ) -> CompressReport:
        cut = find_cut(messages)
        if cut is None:
            report.notice = "没有可压缩的消息（历史太短或切点不合法）"
            return report

        threshold = self.context_window - (0 if report.emergency else SAFETY_AUTO)

        for attempt in range(MAX_DEGRADE_ATTEMPTS):
            chunk = messages[:cut]
            try:
                summary = await summarize(self.provider, chunk, cancel_event)
            except CompressCancelled:
                report.cancelled = True
                return report
            except Exception as e:
                return self._fail(report, e)

            if summary is None:
                return self._fail(report, ValueError("摘要输出不可解析"))

            new_messages = [
                self._summary_message(summary, len(chunk), chunk, tool_names)
            ] + messages[cut:]
            after = self._estimate_total(new_messages)

            last_attempt = attempt == MAX_DEGRADE_ATTEMPTS - 1
            if after < threshold or last_attempt:
                return self._apply(conv, new_messages, cut, total, after, report)

            # 压完仍超线：说明保留区自己就太大，扩大摘要范围再试一次
            nxt = next_cut(messages, cut)
            if nxt is None:
                return self._apply(conv, new_messages, cut, total, after, report)
            cut = nxt

        return report  # pragma: no cover —— 循环内必返回

    def _apply(
        self,
        conv: Conversation,
        new_messages: list[Message],
        cut: int,
        total: int,
        after: int,
        report: CompressReport,
    ) -> CompressReport:
        conv.rewrite(new_messages)
        self.reset_anchor()  # 历史变了，锚点作废，下次由 API 重建
        self.consecutive_failures = 0

        kept = len(new_messages) - 1
        report.summarized = cut
        report.kept = kept
        report.tokens_after = after
        report.info = CompressInfo(
            phase="done",
            tokens_before=total,
            tokens_after=after,
            summarized=cut,
            kept=kept,
            spilled=report.spilled,
        )
        report.notice = (
            f"已压缩：摘要 {cut} 条 / 保留 {kept} 条，tokens {total:,} → {after:,}"
        )
        if after >= self.context_window - SAFETY_AUTO:
            report.notice += "（仍高于触发线：保留区本身过大）"
        return report

    def _fail(self, report: CompressReport, err: Exception) -> CompressReport:
        self.consecutive_failures += 1
        if self.consecutive_failures >= SUMMARY_MAX_FAILURES:
            self.fuse_blown = True
        report.failed = str(err)
        report.notice = f"压缩失败：{err}（连续 {self.consecutive_failures} 次）" + (
            "，自动压缩已熔断" if self.fuse_blown else ""
        )
        return report

    def _summary_message(
        self,
        summary: str,
        summarized_count: int,
        chunk: list[Message],
        tool_names: list[str],
    ) -> Message:
        resume_text = resume.build_resume(chunk, tool_names)
        content = (
            "<context_summary>\n"
            f"{summary}\n"
            "</context_summary>\n\n"
            f"[上下文已压缩] 以上是较早对话的摘要（原 {summarized_count} 条消息，已省略）。"
            "摘要会丢失代码细节与精确数值。\n"
            f"{resume_text}"
        )
        return Message(role="user", content=content)
