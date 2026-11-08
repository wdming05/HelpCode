"""第二层压缩：切点计算 + 结构化摘要。

切点是本章最容易出错的地方：保留区的第一条不能是 tool 消息，
否则它对应的 assistant(tool_calls) 被摘要掉，就成了孤儿 tool → 两个协议都会 400。
"""

import json
import re

from ..llm import Message, Request, System, is_context_too_long
from .constants import (
    KEEP_TAIL_MIN_MSGS,
    KEEP_TAIL_TOKENS,
    MIN_SUMMARIZE_MSGS,
    MIN_SUMMARY_CHARS,
    PTL_RETRY_LIMIT,
    TRUNCATE_BEFORE_SUMMARY,
)
from .estimate import estimate_tokens, message_tokens


class CompressCancelled(Exception):
    """压缩过程中被用户取消（不是 asyncio.CancelledError，避免冒泡到 TUI）。"""


def find_cut(messages: list[Message]) -> int | None:
    """返回保留区起点下标；返回 None 表示「不该摘要」。

    ``messages[:cut]`` 将被摘要掉，``messages[cut:]`` 原样保留。
    """
    if len(messages) <= KEEP_TAIL_MIN_MSGS:
        return None

    i, tok, cnt = len(messages), 0, 0
    while i > 0 and (tok < KEEP_TAIL_TOKENS or cnt < KEEP_TAIL_MIN_MSGS):
        i -= 1
        tok += message_tokens(messages[i])
        cnt += 1

    if i == 0:
        return None  # 全部都得保留，没有可摘要的部分

    return _fix_cut(messages, i)


def next_cut(messages: list[Message], current: int) -> int | None:
    """把切点后移到下一个 user 消息（F9 降级：压一次还超线时扩大摘要范围）。"""
    i = current
    while i < len(messages) and messages[i].role != "user":
        i += 1
    if i >= len(messages):
        return None
    return _fix_cut(messages, i)


def _fix_cut(messages: list[Message], i: int) -> int | None:
    """校正只能后移：保留区变小、待摘要区变大，永远是安全方向。

    前移会让 assistant(tool_calls) 落进摘要区而结果留在保留区 → 孤儿 tool。
    """
    while i < len(messages):
        m = messages[i]
        if m.role == "tool":
            i += 1
            continue
        if m.role == "assistant" and m.tool_calls:
            need = {tc.id for tc in m.tool_calls}
            have = {x.tool_call_id for x in messages[i + 1 :] if x.role == "tool"}
            if not need <= have:
                i += 1
                continue
        return i if i >= MIN_SUMMARIZE_MSGS else None
    return None


# ---------------------------------------------------------------- 摘要请求

SUMMARY_SYSTEM = (
    "你是上下文压缩器。你只输出文本，不能调用任何工具。"
    "你正在为一个 coding agent 压缩对话历史，压缩后原对话将被丢弃。"
)

SUMMARY_USER = """下面是 coding agent 与用户的对话历史（即将被丢弃）。请压缩。

规则：
1. 禁止调用任何工具，只输出文本。
2. 先写 <draft> 段：逐条列出事实、路径、命令、结论，作为你的分析草稿。
3. 再写 <summary> 段：按下面六节组织正式摘要。
4. 不确定的信息写"未确认"，禁止猜测或编造。
5. 用户的原话要求逐条列出，不得改写措辞。

<summary> 的六节：
## 用户诉求
（用户要做什么、有哪些明确约束）
## 已完成
（做过哪些修改、做过什么决定、哪些已验证）
## 关键技术上下文
（文件路径、函数名、命令、数据结构、错误现象——精确到能照着操作）
## 未完成
## 当前阻塞
## 下一步

对话历史：
{chunk}
"""

_SUMMARY_TAG = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)


def parse_summary(raw: str) -> str | None:
    """只取 <summary> 段；草稿永远不返回。取不到或过短视为失败。"""
    m = _SUMMARY_TAG.search(raw)
    if not m:
        return None
    body = m.group(1).strip()
    return body if len(body) >= MIN_SUMMARY_CHARS else None


def build_request(chunk: str) -> Request:
    """摘要请求：不带工具（硬保证模型无法调工具）、不带环境段。"""
    return Request(
        messages=[Message(role="user", content=SUMMARY_USER.format(chunk=chunk))],
        tools=None,
        system=System(stable=SUMMARY_SYSTEM),
        reminder="",
    )


def _truncate_for_summary(text: str) -> str:
    """挖空中间，头尾各留约 1/4。这些消息马上会被摘要掉，截断不损失可用信息。"""
    if estimate_tokens(text) <= TRUNCATE_BEFORE_SUMMARY:
        return text
    keep = max(1, len(text) // 4)
    removed = len(text) - 2 * keep
    return f"{text[:keep]}\n…[已截断 {removed} 字符]…\n{text[-keep:]}"


def _serialize(messages: list[Message]) -> str:
    """把待摘要区压成自然文本（比 JSON 省 token，且模型读得更准）。"""
    parts: list[str] = []
    for idx, m in enumerate(messages, 1):
        if m.role == "assistant" and m.tool_calls:
            calls = ", ".join(
                f"{tc.name}{json.dumps(tc.input, ensure_ascii=False)}"
                for tc in m.tool_calls
            )
            parts.append(f"[{idx}] assistant(tool_calls: {calls}):")
        elif m.role == "tool":
            parts.append(f"[{idx}] tool: {_truncate_for_summary(m.content)}")
        else:
            parts.append(f"[{idx}] {m.role}: {m.content}")
    return "\n".join(parts)


def _groups(messages: list[Message]) -> list[tuple[int, int]]:
    """切成「消息组」：[start, end)。一组 = 一条 user，或一条 assistant + 它的全部结果。

    摘要请求超长时按组丢弃：丢单条会切开 assistant 与它的 tool 结果，
    让摘要模型看到孤儿结果并编造上下文。
    """
    ranges: list[tuple[int, int]] = []
    i = 0
    n = len(messages)
    while i < n:
        start = i
        m = messages[i]
        i += 1
        if m.role == "assistant" and m.tool_calls:
            while i < n and messages[i].role == "tool":
                i += 1
        ranges.append((start, i))
    return ranges


async def summarize(provider, messages: list[Message], cancel_event=None) -> str | None:
    """生成摘要；返回 None 表示「模型输出不可解析」（上层计入熔断）。

    摘要请求自身也可能超窗口，所以带「丢中间最老一组 → 重试」的循环，
    上限 PTL_RETRY_LIMIT 次，绝不无限重试。

    **groups[0]（用户原始诉求）永不丢**：它是整份摘要的锚点，一旦被丢，
    摘要模型就不知道「用户到底要什么」。
    """
    groups = _groups(messages)
    if not groups:
        return None
    head = groups[0]  # 第一组：用户原始诉求，永远保留在 chunk 最前
    skip = 1          # 从第 1 组开始纳入；撞墙时递增，丢「中间最老」的一组
    omitted_msgs = 0

    last_exc: Exception | None = None
    for _attempt in range(PTL_RETRY_LIMIT + 1):
        parts = [_serialize(messages[head[0] : head[1]])]  # 原始诉求永远在最前
        if omitted_msgs:
            parts.append(
                f"（中间有 {omitted_msgs} 条消息因超出上下文被省略，未纳入本次摘要）"
            )
        if skip < len(groups):
            parts.append(_serialize(messages[groups[skip][0] :]))
        chunk = "\n".join(parts)

        raw = ""
        try:
            async for ev in provider.stream(build_request(chunk)):
                if cancel_event is not None and cancel_event.is_set():
                    raise CompressCancelled
                if ev.err is not None:
                    raise ev.err
                if ev.text:
                    raw += ev.text
                if ev.done:
                    break
        except CompressCancelled:
            raise
        except Exception as e:
            if not is_context_too_long(e) or skip >= len(groups):
                raise
            last_exc = e
            omitted_msgs += groups[skip][1] - groups[skip][0]
            skip += 1
            continue

        return parse_summary(raw)

    raise last_exc or RuntimeError("摘要请求失败")
