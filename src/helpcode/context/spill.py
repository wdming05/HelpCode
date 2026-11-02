"""第一层压缩：把超大的工具结果写成文件，对话里只留「预览 + 路径」。

为什么不压缩对话：coding agent 的 token 大头是工具结果（文件内容、命令输出），
对话本身通常只有几百 token。所以第一层只动 ``role == "tool"`` 的消息。

为什么不把文件写到系统临时目录：ch06 的沙箱只允许读项目根内的路径，
写到项目外模型就 ``read_file`` 不回来 —— 等于把数据丢了。
"""

import copy
import re
from datetime import datetime
from pathlib import Path

from ..conversation import Conversation
from ..llm import Message
from ..tool import ToolResult
from .constants import (
    BATCH_RESULT_MAX,
    PREVIEW_CHARS,
    PREVIEW_LINES,
    SPILL_DIR,
    SPILL_EXEMPT_TOOLS,
    SPILL_MARK,
    TOOL_RESULT_MAX,
)
from .estimate import estimate_tokens

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def apply(
    conv: Conversation, root: Path, subdir: Path | None = None
) -> tuple[int, list[str]]:
    """就地落盘超限的工具结果，返回 (落盘条数, 告警列表)。

    不抛异常：落盘失败（磁盘满 / 只读目录）只是让这一条保持原样并记一条告警，
    因为「超预算」比「对话直接崩掉」轻得多。
    """
    messages = copy.deepcopy(conv.messages())
    call_names = _collect_call_names(messages)

    spilled = 0
    warnings: list[str] = []

    for group in _groups(messages):
        sizes = {i: estimate_tokens(messages[i].content) for i in group}
        total = sum(sizes.values())
        # 豁免工具（read_file）的结果不落盘：它落盘后模型会再读回来，循环。
        # 注意 total 仍把这部分算进去 —— 它确实占着预算，所以其他工具会更早被落盘。
        exempt = {
            i
            for i in group
            if call_names.get(messages[i].tool_call_id or "", "") in SPILL_EXEMPT_TOOLS
        }

        # ① 单条超限
        targets = [
            i
            for i in group
            if sizes[i] > TOOL_RESULT_MAX
            and not _already_spilled(messages[i])
            and i not in exempt
        ]
        for i in targets:
            total -= sizes[i]

        # ② 合计仍超限：在其余消息里按大小降序补
        if total > BATCH_RESULT_MAX:
            rest = sorted(
                (i for i in group if i not in targets and i not in exempt),
                key=lambda i: sizes[i],
                reverse=True,
            )
            for i in rest:
                if total <= BATCH_RESULT_MAX:
                    break
                if _already_spilled(messages[i]):
                    continue
                targets.append(i)
                total -= sizes[i]

        # ③ 按索引顺序写盘（保证文件命名与消息顺序一致，便于排查）
        for i in sorted(targets):
            msg = messages[i]
            name = call_names.get(msg.tool_call_id or "", "tool")
            preview = _spill_one(msg, name, root, warnings, subdir)
            if preview is None:
                continue
            msg.content = preview
            if msg.tool_results:
                msg.tool_results = [
                    ToolResult(ok=r.ok, content=preview, is_error=r.is_error)
                    for r in msg.tool_results
                ]
            spilled += 1

    if spilled:
        conv.rewrite(messages)
    return spilled, warnings


def _collect_call_names(messages: list[Message]) -> dict[str, str]:
    """tool_call_id → 工具名（tool 消息自己不带名字，要从 assistant 反查）。"""
    names: dict[str, str] = {}
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                names[tc.id] = tc.name
    return names


def _groups(messages: list[Message]) -> list[list[int]]:
    """按「一条 assistant(tool_calls) → 其后连续的 tool 消息」切组，返回索引组。"""
    groups: list[list[int]] = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if m.role == "assistant" and m.tool_calls:
            j = i + 1
            idx: list[int] = []
            while j < n and messages[j].role == "tool":
                idx.append(j)
                j += 1
            if idx:
                groups.append(idx)
            i = j
        else:
            i += 1
    return groups


def _already_spilled(msg: Message) -> bool:
    return msg.content.startswith(SPILL_MARK)


def _spill_one(
    msg: Message,
    tool_name: str,
    root: Path,
    warnings: list[str],
    subdir: Path | None = None,
) -> str | None:
    """写盘并返回替换后的预览文本；失败返回 None 并追加告警。"""
    content = msg.content
    target = _target_path(root, tool_name, msg.tool_call_id or "", subdir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except Exception as e:
        # 不能只捕 OSError：编码错误（UnicodeEncodeError）不是 OSError，
        # 漏出去会让整轮对话崩在「压缩」这一步，而它本来只是个可失败的后台动作。
        warnings.append(f"工具结果落盘失败（{target.name}）：{type(e).__name__}: {e}")
        return None

    lines = content.splitlines()
    # 行 + 字符双闸：只按行截时，单行超长（minified JSON / 单行 CSV）的
    # 「前 20 行预览」就是全文，落盘等于没做。
    raw_preview = "\n".join(lines[:PREVIEW_LINES])
    chars_cut = len(raw_preview) > PREVIEW_CHARS
    preview = raw_preview[:PREVIEW_CHARS]
    rel = target.relative_to(root).as_posix()
    tail = (
        f"...（预览已截断：仅保留前 {PREVIEW_LINES} 行中的前 {PREVIEW_CHARS} 字符）"
        if chars_cut
        else "...（预览结束）"
    )
    return (
        f"{SPILL_MARK} 原长 {len(lines)} 行 / 约 {estimate_tokens(content)} tokens\n"
        f"文件: {rel}\n"
        f"前 {PREVIEW_LINES} 行预览:\n"
        f"{preview}\n"
        f"{tail}\n"
        "需要完整内容请用 read_file 工具读取上面的相对路径（可配合 offset/limit 分段读）。\n"
        "不要凭预览推断未看到的部分。"
    )


def _target_path(
    root: Path, tool_name: str, call_id: str, subdir: Path | None = None
) -> Path:
    """生成不覆盖已有文件的落盘路径。

    ``subdir`` 由装配层注入（会话目录下的 ``tool-results/``）。**相对基准始终是
    ``root``**——消息里写给模型的路径必须是「相对项目根」的完整路径，
    因为 ``read_file`` 以项目根为工作目录。写成相对会话目录的
    ``tool-results/x.txt``，模型会读不到（F12/AC39）。

    ``subdir`` 为空时退回全局目录：会话建立失败不该连带打断压缩。
    """
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    folder = subdir if subdir is not None else SPILL_DIR
    base = folder / f"{ts}-{_slug(tool_name)}-{_slug(call_id)[:6]}.txt"
    target = root / base
    n = 2
    while target.exists():
        target = root / base.with_name(f"{base.stem}-{n}{base.suffix}")
        n += 1
    return target


def _slug(text: str) -> str:
    return _UNSAFE.sub("-", text).strip("-")[:40] or "x"
