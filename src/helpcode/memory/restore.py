"""会话回放：坏行、孤儿、未完成组、时间跨度。

四条原则：

1. **能救就救，救不了就丢**。一行坏了跳过它，不影响其它行；一条 tool 找不到
   调用就丢弃它，不因此放弃整个会话。
2. **补位而非删除**。未完成的工具调用不整组丢弃，而是给缺的 id 补一条
   「执行被中断」的结果：丢掉整组会让模型失去「上一轮做到哪儿」的线索，
   补一条措辞谨慎的结果既满足协议配对，又明确告诉它「状态未知，得去核实」。
   措辞不能说满——工具可能压根没启动，也可能已经改了一半，断言任何一端都是错的。
3. **孤儿结果直接丢**。找不到对应调用的 tool 消息留着只会让请求被拒。
4. **恢复出来的历史必须能被 API 接受**。首条不能是 tool，不能有孤立的
   tool_call_id，否则两个协议都会直接 400。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ..llm import Message, ToolCall
from .constants import TIME_GAP_HOURS, TIME_GAP_MARK

# 中断结果的措辞：刻意停在「不确定」，不说「已失败」也不说「未执行」。
# 工具可能压根没启动，也可能已经改了一半——写死任何一端都是在编造事实，
# 而模型会照着这句话决定要不要重跑，所以必须把核实动作交回给它。
INTERRUPTED_RESULT = (
    "执行被中断，产生的实际影响需要自行确认："
    "它可能尚未开始，也可能已经部分生效。"
    "继续之前请核实相关文件或命令的真实状态，不要假定什么都没有发生。"
)


@dataclass
class RestoreReport:
    messages: list[Message] = field(default_factory=list)
    skipped_lines: int = 0
    interrupted_groups: int = 0
    filled_results: int = 0
    last_ts: datetime | None = None
    warnings: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        """修复汇总行；没有发生修复时返回空串（不占屏幕也不打扰人）。"""
        parts: list[str] = []
        if self.skipped_lines:
            parts.append(f"跳过 {self.skipped_lines} 行损坏记录")
        if self.interrupted_groups:
            parts.append(
                f"{self.interrupted_groups} 组调用被中断，"
                f"已补 {self.filled_results} 条待核实结果"
            )
        if not parts:
            return ""
        return "（恢复时" + "，".join(parts) + "）"


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def restore(jsonl: Path) -> RestoreReport:
    """把 jsonl 回放成消息列表。

    只回放**最后一个** ``{"type":"compact"}`` 之后的内容——标记之前的行在语义上
    已经作废（它们被压缩掉了），但仍然留在文件里，因为存档是纯追加的。

    时间跨度提醒**不在这里追加**：本函数只交出 ``last_ts``，由装配层调
    ``time_gap_notice`` 决定要不要提醒。分开的理由是「回放」和「提醒」是两个
    关注点——回放要能在测试里纯粹地断言历史，不该混进一条与时间有关的消息。
    """
    report = RestoreReport()
    try:
        text = jsonl.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return report  # 文件不存在：新会话，空历史

    records: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            report.skipped_lines += 1
            continue
        if isinstance(record, dict):
            records.append(record)

    # 从最后一个 compact 标记之后开始。判定用 type 字段，不靠「没有 role」推断——
    # 将来加别的记录类型（type: "note"）时不会误判。
    start = 0
    for i, record in enumerate(records):
        if record.get("type") == "compact":
            start = i + 1
    records = records[start:]

    results: list[Message] = []
    # 已声明、尚未收到结果的 call id。用 list 而不是 set：补位时要按声明顺序
    # 追加，顺序错乱的 tool 块虽然多数协议能接受，但排查问题时读起来很别扭。
    pending: list[str] = []

    def fill_pending_group() -> None:
        """给未收齐的调用补一条「执行被中断」结果；已收到的原样保留。

        不删整组：assistant 那条消息里的 tool_calls 记录了模型上一轮的真实意图，
        删掉等于篡改历史；留下已收到的结果则能告诉模型哪几步是确实跑过的。
        """
        if not pending:
            return
        for call_id in pending:
            results.append(
                Message(
                    role="tool",
                    content=INTERRUPTED_RESULT,
                    tool_call_id=call_id,
                )
            )
            report.filled_results += 1
        report.interrupted_groups += 1
        pending.clear()

    for record in records:
        role = record.get("role")

        if role == "assistant":
            fill_pending_group()

            raw_calls = record.get("tool_calls") or []
            calls = [
                ToolCall(
                    id=str(c.get("id", "")),
                    name=str(c.get("name", "")),
                    input=c.get("input") or {},
                )
                for c in raw_calls
                if isinstance(c, dict) and c.get("id")
            ]
            results.append(
                Message(
                    role="assistant",
                    content=str(record.get("content") or ""),
                    tool_calls=calls or None,
                )
            )
            if calls:
                pending = [c.id for c in calls]

        elif role == "tool":
            call_id = record.get("tool_call_id")
            if call_id and call_id in pending:
                results.append(
                    Message(
                        role="tool",
                        content=str(record.get("content") or ""),
                        tool_call_id=str(call_id),
                    )
                )
                pending.remove(str(call_id))
            else:
                # 孤儿结果：没有对应的调用，留着会让 API 报 400
                report.skipped_lines += 1

        elif role == "user":
            fill_pending_group()
            results.append(
                Message(role="user", content=str(record.get("content") or ""))
            )

        else:
            report.skipped_lines += 1
            continue

        ts = _parse_ts(record.get("ts"))
        if ts is not None:
            report.last_ts = ts

    fill_pending_group()

    # 防御：历史不能以 tool 开头（前一行被跳过时可能出现）
    while results and results[0].role == "tool":
        results.pop(0)
        report.skipped_lines += 1

    report.messages = results
    return report


def time_gap_notice(last_ts: datetime, now: datetime) -> Message | None:
    """距上次对话**满** ``TIME_GAP_HOURS`` 小时时，返回一条提醒消息。

    提醒以 ``TIME_GAP_MARK`` 开头，写存档时会被过滤掉（D8）：它是「这一次会话的
    提示」，不是历史事实。存进去会在下次恢复时再叠一条，一轮轮堆下去。
    """
    if last_ts.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif last_ts.tzinfo is not None and now.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=None)

    delta = now - last_ts
    if delta < timedelta(hours=TIME_GAP_HOURS):
        return None

    return Message(
        role="user",
        content=(
            f"{TIME_GAP_MARK}上次对话在 {_relative(delta)}前。"
            "细节可能已变化，需要时请重新读取文件，不要照记忆里的内容推断当前代码。"
        ),
    )


def _relative(delta: timedelta) -> str:
    days = delta.days
    if days >= 1:
        return f"{days} 天"
    hours = max(1, int(delta.total_seconds() // 3600))
    return f"{hours} 小时"
