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
from . import micro, resume, spill
from .constants import (
    BUFFER_TOTAL,
    HARD_WATERMARK,
    KEEP_TAIL_MIN_MSGS,
    MICRO_CHAIN_MIN,
    MICRO_WATERMARK,
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

        # 有效窗口 = 原始窗口 − 33K 缓冲（下限保护：小窗口不退化成负值）
        self.effective_window = max(
            context_window - BUFFER_TOTAL, int(context_window * 0.5)
        )
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
        """纯估算，不碰 LLM；供 agent 在压缩前先发一个 start 事件。

        只预告 L5 全局硬摘要（≥80% 有效窗口）——L4 微压缩是轻量静默动作，
        不值得 start/done 两段事件。
        """
        if not self.auto_enabled:
            return False
        return self.estimate(conv) >= self.effective_window * HARD_WATERMARK

    def has_room_after_compress(self, tokens_after: int) -> bool:
        """紧急压缩后是否还值得重试：贴着窗口就说明压不动，重试必然再撞。"""
        return tokens_after < self.context_window - SAFETY_MANUAL

    # ------------------------------------------------------------ 入口

    async def before_request(
        self, conv: Conversation, cancel_event, tool_names: list[str]
    ) -> CompressReport:
        """每次 API 请求之前：先落盘，再按需摘要。"""
        # 扫描 conv 里的工具结果，超过阈值的写到磁盘，对话里只留预览和路径。
        # L1 阈值用有效窗口比例（自适应不同窗口的模型）。
        spilled, warnings = spill.apply(
            conv, self.root, self._spill_subdir, self.effective_window
        )
       
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

        # L4 局部微压缩：达 55% 有效窗口时，摘要最早的大工具链（静默轻量）
        total = await self._maybe_micro_compress(conv, cancel_event, report, total)
        if report.cancelled:
            return report

        # L5 全局硬摘要：达 80% 有效窗口时，全量重写摘要
        if total < self.effective_window * HARD_WATERMARK:
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

    async def _maybe_micro_compress(
        self,
        conv: Conversation,
        cancel_event,
        report: CompressReport,
        total: int,
    ) -> int:
        """L4 局部微压缩：水位 ≥55% 时摘要最早的大工具链。返回压后的 total。

        失败**静默降级**（返回原 total），由 L5 硬摘要兜底——L4 是「免费的轻量优化」，
        不值得为它计入熔断。只动工具链、不碰用户/助手自然语言，保住对话连贯性。
        """
        if total < self.effective_window * MICRO_WATERMARK:
            return total

        messages = conv.messages()
        chains = micro.find_tool_chains(messages)
        if not chains:
            return total

        start, end = chains[0]  # 最早的一条链
        if micro.chain_tokens(messages, chains[0]) < MICRO_CHAIN_MIN:
            return total

        chain = messages[start : end + 1]
        try:
            summary = await self._summarize_chain(chain, cancel_event)
        except CompressCancelled:
            report.cancelled = True
            return total
        except Exception as e:
            logger.warning("L4 局部微压缩失败：%s", e)
            return total

        if not summary:
            return total

        new_messages = micro.replace_chain(messages, start, end, summary)
        conv.rewrite(new_messages)
        self.reset_anchor()  # 历史变了，锚点作废，下次由 API 重建
        after = self._estimate_total(new_messages)
        report.tokens_after = after
        report.notice = f"已局部微压缩：摘要早期工具链（{end - start + 1} 条 → 1 条）"
        return after

    async def _summarize_chain(
        self, chain: list[Message], cancel_event
    ) -> str | None:
        """调 LLM 把一条工具链摘要成一段短文本；不可解析返回 None。"""
        chunk = micro.serialize_chain(chain)
        raw = ""
        async for ev in self.provider.stream(micro.build_micro_request(chunk)):
            if cancel_event is not None and cancel_event.is_set():
                raise CompressCancelled
            if ev.err is not None:
                raise ev.err
            if ev.text:
                raw += ev.text
            if ev.done:
                break
        return micro.parse_micro_summary(raw)

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
        # L5 附件通道：从文件系统读可靠快照（git 分支/改动），不走 LLM、不压缩
        attach = resume.snapshot_system_state(self.root)
        content = (
            "<context_summary>\n"
            f"{summary}\n"
            "</context_summary>\n\n"
            f"[上下文已压缩] 以上是较早对话的摘要（原 {summarized_count} 条消息，已省略）。"
            "摘要会丢失代码细节与精确数值。\n"
            f"{resume_text}"
        )
        if attach:
            content += f"\n\n[状态附件]\n{attach}"
        return Message(role="user", content=content)
