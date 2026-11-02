"""自动笔记：从对话里提炼可复用的知识，落成带 frontmatter 的 Markdown。

四类笔记（见 ``constants.NOTE_TYPES``）分别解决不同问题：

- ``user_preference``：用户怎么做事（"提交前先跑 ruff"）
- ``correction_feedback``：用户纠正过什么（"不要用 os.path，用 pathlib"）
- ``project_knowledge``：这个项目的结构约定（"入口在 cli.py，别改 __main__"）
- ``reference_material``：外部资料（文档链接、接口约定）

**提炼请求独立于主对话**：``tools=None``、独立 ``Request``、不写主历史、
不计入用户可见的 token 统计。否则笔记会污染上下文，且用户会看到莫名其妙的消耗。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..llm import Message, Request, System
from .constants import (
    NOTE_CHUNK_CHARS,
    NOTE_CHUNK_MSGS,
    NOTE_SLUG_MAX,
    NOTE_TYPES,
)

logger = logging.getLogger(__name__)

# 并发保护：索引是「读 → 改 → 写」。两个任务同时读到同一份旧索引，
# 各自生成笔记，后写的会覆盖先写的——静默丢掉一条（D22）。
# 锁必须包住整段（含 LLM 调用），否则两个任务基于同一份旧索引各写一份。
_lock = asyncio.Lock()

NOTE_SYSTEM = """你在维护一个编程助手助手的长期记忆。根据「最近对话」提炼值得长期保留的内容。

只记四类：
- user_preference：用户稳定偏好（工具选择、代码风格、沟通方式）
- correction_feedback：用户纠正过你的地方（错误做法 → 正确做法）
- project_knowledge：这个项目的结构、约定、坑
- reference_material：外部资料要点（文档、接口、协议）

判断标准很严：**只记下次还会用到的**。一次性的任务细节、临时文件路径、
「用户让我改这行」都不记。没有值得记的就返回空数组 []。

已有记忆（不要重复添加，内容有更新时用 update 并给出对应 file）：
{index}

输出要求：
- 只输出一个 JSON 数组，不要任何解释文字、不要 Markdown 代码围栏
- 每个元素：{"action": "add"|"update"|"skip", "type": "四类之一", "target": "user"|"project",
  "title": "简短标题", "description": "一句话说明", "body": "正文，可多行",
  "file": "update 时的已有文件名"}
- target 规则：跨项目通用的偏好 → user；只对当前项目成立 → project"""


@dataclass(frozen=True)
class NoteUpdate:
    action: str  # "add" | "update" | "skip"
    type: str
    target: str = "project"
    title: str = ""
    description: str = ""
    body: str = ""
    file: str = ""


def _chunk_messages(chunk: list[Message]) -> str:
    """把最近若干条消息压成一段文本，带条数与字符双上限。"""
    recent = chunk[-NOTE_CHUNK_MSGS:]
    lines: list[str] = []
    total = 0
    for msg in recent:
        text = msg.content.replace("\n", " ").strip()
        if msg.tool_calls:
            names = ", ".join(tc.name for tc in msg.tool_calls)
            text = f"{text} [调用工具: {names}]".strip()
        line = f"{msg.role}: {text}"
        if total + len(line) > NOTE_CHUNK_CHARS:
            line = line[: max(0, NOTE_CHUNK_CHARS - total)]
        lines.append(line)
        total += len(line)
        if total >= NOTE_CHUNK_CHARS:
            break
    return "\n".join(lines)


def build_request(chunk: list[Message], existing_index: str = "") -> Request:
    """构造提炼请求：``tools=None``、独立 system、单条 user 消息。"""
    system = NOTE_SYSTEM.replace("{index}", existing_index.strip() or "（暂无）")
    return Request(
        messages=[
            Message(
                role="user",
                content=f"最近对话：\n{_chunk_messages(chunk)}",
            )
        ],
        tools=None,
        system=System(stable=system),
        reminder="",
    )


_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


def parse_updates(raw: str) -> list[NoteUpdate]:
    """解析模型输出。

    容错策略：先用正则抓第一个 ``[...]`` 块（模型常会加解释或代码围栏），
    再逐项校验；单项非法只丢该项，不影响其它项。整段解析不出来返回空列表——
    「这一轮没提炼出东西」是正常结果，不是错误。
    """
    match = _JSON_ARRAY.search(raw or "")
    if not match:
        return []

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.debug("笔记输出不是合法 JSON：%s", raw[:200])
        return []

    if not isinstance(data, list):
        return []

    updates: list[NoteUpdate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", "")).lower()
        if action not in {"add", "update", "skip"}:
            continue
        if action == "skip":
            continue

        note_type = str(item.get("type", "")).strip()
        if note_type not in NOTE_TYPES:
            continue  # 类型不合法：宁可不记，也不往错的分类里塞

        target = str(item.get("target", "project")).lower()
        if target not in {"user", "project"}:
            target = "project"

        title = str(item.get("title", "")).strip()
        if not title:
            continue

        updates.append(
            NoteUpdate(
                action=action,
                type=note_type,
                target=target,
                title=title,
                description=str(item.get("description", "")).strip(),
                body=str(item.get("body", "")).strip(),
                file=str(item.get("file", "")).strip(),
            )
        )
    return updates


# ---------------------------------------------------------------- 写入


INDEX_NAME = "index.md"


def slugify(title: str) -> str:
    """标题 → 文件名主干。

    三条规则保证「同一标题永远同一个文件名」：
    小写、把非字母数字换成 `-`、截断到 ``NOTE_SLUG_MAX``。

    **不带时间戳、不带序号**——否则同一个标题每次都会生成新文件，
    「去重」就变成「每次加一份副本」（AC28）。
    """
    lowered = title.strip().lower()
    dashed = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", lowered).strip("-")
    if not dashed:
        return "note"
    return dashed[:NOTE_SLUG_MAX].strip("-") or "note"


def _frontmatter(note_type: str, title: str, created: str, updated: str) -> str:
    return (
        "---\n"
        f"type: {note_type}\n"
        f"title: {title}\n"
        f"created: {created}\n"
        f"updated: {updated}\n"
        "---\n"
    )


def _read_created(path: Path) -> str | None:
    """从已有文件里取出 created，更新时保留原始创建时间。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^created:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _read_title(path: Path) -> str | None:
    """从已有文件里取出 title。删除笔记时用它定位索引行（索引行不再含文件名）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _index_line(note_type: str, title: str, description: str) -> str:
    """索引行格式：``- [类型] 标题 — 一句话描述``（F8）。

    第三段必须是**描述**而不是文件名。文件名是 ``提交前跑-ruff.md`` 这种
    slug，模型只能看出「大概跟 ruff 有关」；而描述是「提交前必须跑 ruff」，
    它才是决定「要不要展开这条」的依据。两者信息量差一个量级。
    """
    return f"- [{note_type}] {title} — {description or title}"


def _same_note(title: str) -> str:
    """在一行索引里认出「同一条笔记」的片段。

    用 ``] 标题 —``（带行内结构）而不是裸标题：裸标题会让「测试」那一行
    被「测试约定」那一行误删。
    """
    return f"] {title} —"


def _upsert_index(store: Path, line: str, title: str) -> None:
    """把一行插到索引最前面，去掉同一标题的旧行。

    插到最前而不是末尾：``index.cap`` 裁剪时保留前 N 行，所以「位置即优先级」，
    最近的笔记必须排在最前，才不会被裁掉（D11）。
    """
    index_path = store / INDEX_NAME
    try:
        existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    except OSError:
        existing = ""

    marker = _same_note(title)
    kept = [ln for ln in existing.splitlines() if ln.strip() and marker not in ln]
    index_path.write_text("\n".join([line, *kept]) + "\n", encoding="utf-8")


def _remove_from_index(store: Path, title: str) -> None:
    index_path = store / INDEX_NAME
    if not index_path.exists() or not title:
        return
    try:
        lines = index_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    marker = _same_note(title)
    kept = [ln for ln in lines if marker not in ln]
    index_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def apply(
    updates: list[NoteUpdate],
    home_memory: Path,
    project_memory: Path,
    *,
    now: datetime | None = None,
) -> list[str]:
    """执行写入，返回告警列表（写失败不抛，笔记是增强不是前提）。"""
    warnings: list[str] = []
    stamp = (now or datetime.now()).isoformat(timespec="seconds")

    for update in updates:
        store = project_memory if update.target == "project" else home_memory
        filename = update.file or f"{slugify(update.title)}.md"
        path = store / filename

        try:
            store.mkdir(parents=True, exist_ok=True)

            if update.action == "delete":
                # 索引行里已经没有文件名了，只能从笔记的 frontmatter 反查标题
                title = _read_title(path) or update.title
                path.unlink(missing_ok=True)
                _remove_from_index(store, title)
                continue

            created = _read_created(path) or stamp
            body = update.body or update.description or update.title
            path.write_text(
                _frontmatter(update.type, update.title, created, stamp) + f"\n{body}\n",
                encoding="utf-8",
            )
            _upsert_index(
                store,
                _index_line(update.type, update.title, update.description),
                update.title,
            )
        except OSError as exc:
            warnings.append(f"{path}: 笔记写入失败（{exc.__class__.__name__}），已跳过")

    return warnings


# ---------------------------------------------------------------- 编排


async def update(
    provider,
    chunk: list[Message],
    existing_index: str,
    home_memory: Path,
    project_memory: Path,
    *,
    now: datetime | None = None,
) -> list[NoteUpdate]:
    """提炼一轮并写入。

    整段（构造请求 → 调 LLM → 解析 → 写文件 → 改索引）都在同一把锁里。
    只锁写文件是不够的：两个任务会基于**同一份旧索引**各自生成笔记，
    第二个写回时把第一个的内容覆盖掉——而索引看起来完全正常，只是少了一条。

    任何异常都吞掉并返回空列表：笔记是增强，失败不该影响对话，也不该在
    UI 上弹错误。
    """
    async with _lock:
        request = build_request(chunk, existing_index)

        parts: list[str] = []
        try:
            async for event in provider.stream(request):
                if event.err is not None:
                    raise event.err
                if event.text:
                    parts.append(event.text)
        except Exception as exc:  # 提炼失败不该打断任何东西
            logger.warning("笔记提炼失败（%s），本轮跳过", exc.__class__.__name__)
            return []

        updates = parse_updates("".join(parts))
        for warning in apply(updates, home_memory, project_memory, now=now):
            logger.warning("%s", warning)
        return updates
