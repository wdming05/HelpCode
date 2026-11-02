"""会话目录布局与 ID 时间解析。

目录形状（F12：会话与它产生的落盘文件绑在一起）：

```
<root>/.helpcode/sessions/<YYYYMMDD-HHMMSS-xxxx>/
    conversation.jsonl
    tool-results/
```

为什么是「一个会话一个目录」而不是「一个大目录里一堆 jsonl」：
删会话要连带删它的落盘文件，否则会留下孤儿；两套过期规则也说不清谁管谁。

**本模块是全章唯一的时间来源**：排序、过期判定、列表展示全部走
``parse_session_time``，任何地方都不要再读 ``st_mtime``。理由见 D23。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Self, TextIO

from ..llm import Message
from .constants import (
    JSONL_NAME,
    LEGACY_TOOL_RESULTS,
    MAX_MODEL_LEN,
    SESSIONS_DIR,
    TIME_GAP_MARK,
    TOOL_RESULTS_NAME,
)

logger = logging.getLogger(__name__)

# 会话 ID 的固定前缀长度："YYYYMMDD-HHMMSS"
_ID_TIME_CHARS = 15
_ID_TIME_FMT = "%Y%m%d-%H%M%S"

# 扫描不到 model 时的展示占位。它**不是**模型名：cli 恢复时要把这个值挡掉，
# 否则会把「未知」当成模型写进新追加的行里，越传越脏。
UNKNOWN_MODEL = "未知"


def new_session_id(now: datetime | None = None) -> str:
    """生成 ``YYYYMMDD-HHMMSS-xxxx``。

    4 位随机十六进制是为了防「同秒创建两个会话」——用户连开两个终端、
    或者测试里连建两个会话，都可能落在同一秒。
    """
    stamp = (now or datetime.now()).strftime(_ID_TIME_FMT)
    return f"{stamp}-{secrets.token_hex(2)}"


def parse_session_time(session_id: str) -> datetime:
    """从 ID 前 15 位解析创建时刻（本地时间，naive）。

    只切前 15 位，后缀完全不参与解析——这样把随机段换成任意字符都不影响，
    将来后缀从 4 位改成 6 位也不用改这里。

    格式不对时**抛 `ValueError`**，不返回 `None`：让每个调用点都必须显式
    ``try/except``，漏处理是看得见的（`None` 会被静默当成「永不过期」）。
    """
    return datetime.strptime(session_id[:_ID_TIME_CHARS], _ID_TIME_FMT)


def sessions_root(root: Path) -> Path:
    return root / SESSIONS_DIR


def session_dir(root: Path, session_id: str) -> Path:
    return sessions_root(root) / session_id


def jsonl_path(root: Path, session_id: str) -> Path:
    return session_dir(root, session_id) / JSONL_NAME


def tool_results_dir(root: Path, session_id: str | None) -> Path:
    """落盘目录：有会话用会话内的，没有则退回 ch08 的全局目录。

    退回是必要的：会话建立失败（只读磁盘、ID 生成异常）不该连带打断压缩——
    存档是增强，不是前提。
    """
    if session_id:
        return session_dir(root, session_id) / TOOL_RESULTS_NAME
    return root / LEGACY_TOOL_RESULTS


def ensure_session_dir(root: Path, session_id: str) -> Path:
    """建会话目录（含 tool-results），返回目录路径。

    ``OSError`` 向上抛：由调用方决定怎么降级（通常是禁用存档、继续对话），
    这里不替它决定。
    """
    directory = session_dir(root, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / TOOL_RESULTS_NAME).mkdir(parents=True, exist_ok=True)
    return directory


# ---------------------------------------------------------------- 写存档


class SessionWriter:
    """向 ``conversation.jsonl`` 追加写。

    三条铁律：

    1. **只追加**，从不读回、从不重写。所以崩溃最多丢最后一行。
    2. **每条都 fsync**。只 ``flush`` 能防进程被杀，防不了断电——而用户真正
       在意的恰是「我昨天写的东西还在」。
    3. **写失败不反噬**。磁盘满 / 只读时告警一次并禁用本会话存档，对话继续。
       存档是增强，不是前提。
    """

    def __init__(self, jsonl: Path, model: str = "") -> None:
        self._jsonl = jsonl
        self._model = model[:MAX_MODEL_LEN] if model else ""
        self._disabled = False
        self._fh: TextIO | None = None
        try:
            jsonl.parent.mkdir(parents=True, exist_ok=True)
            self._fh = jsonl.open("a", encoding="utf-8", newline="\n")
        except OSError as exc:
            self._disable(f"无法打开存档 {jsonl}（{exc.__class__.__name__}）")

    # ------------------------------------------------------------ 属性

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def jsonl(self) -> Path:
        return self._jsonl

    def set_model(self, model: str) -> None:
        """补上模型名，之后写的每行都带它。

        新建会话时 provider 还没选，构造 writer 只能传空串。而 spec F4/AC41 要求
        **每一行**都记 model——恢复时列表要显示「这个会话最后用的模型」，缺一行
        就可能显示成「未知」。所以 provider 一旦确定就得回填。
        """
        self._model = model[:MAX_MODEL_LEN] if model else ""

    # ------------------------------------------------------------ 写入

    def append(self, msg: Message) -> None:
        """追加一条消息。"""
        record = self._to_record(msg)
        self._write(record)

    def write_compact_marker(self) -> None:
        """写一行「此前的记录作废」。

        这一行**不带任何正文**。压缩后的历史可能有几万 token，如果把它塞进
        单行，会到数 MB：fsync 变慢、崩溃时丢的是整份历史、还可能撞上行长限制。
        标记 + 逐条追加让每行仍然很小。
        """
        self._write({"type": "compact", "ts": _now_iso()})

    def append_all(self, messages: list[Message]) -> None:
        """压缩后重建历史：逐条追加，跳过时间跨度提醒。

        提醒是「这一次会话的提示」，不是历史事实。写进存档会在下次恢复时
        再叠一条，一轮轮堆下去（D8）。
        """
        for msg in messages:
            if msg.content.startswith(TIME_GAP_MARK):
                continue
            self.append(msg)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            finally:
                self._fh = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------ 内部

    def _to_record(self, msg: Message) -> dict:
        record: dict = {
            "role": msg.role,
            "content": msg.content,
            "ts": _now_iso(),
        }
        if self._model:
            record["model"] = self._model
        if msg.tool_calls:
            record["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "input": tc.input}
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id:
            record["tool_call_id"] = msg.tool_call_id
        return record

    def _write(self, record: dict) -> None:
        if self._disabled or self._fh is None:
            return
        try:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            _flush(self._fh)
            _fsync(self._fh)
        except (OSError, ValueError) as exc:
            # ValueError：对已关闭的文件写入。与 OSError 同样降级处理。
            self._disable(f"写入存档失败（{exc.__class__.__name__}），本会话存档已停用")

    def _disable(self, reason: str) -> None:
        if not self._disabled:
            logger.warning("%s", reason)
        self._disabled = True
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def _flush(fh: TextIO) -> None:
    """把 Python 缓冲推进操作系统缓冲。"""
    fh.flush()


def _fsync(fh: TextIO) -> None:
    """把操作系统缓冲推进磁盘。少了这步，进程被杀能保住、断电保不住。"""
    os.fsync(fh.fileno())


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 扫描


@dataclass(frozen=True)
class SessionMeta:
    """列表展示所需的全部信息，全部由扫描得出（没有 meta 文件）。"""

    session_id: str
    dir: Path
    jsonl: Path
    title: str
    messages: int
    model: str
    updated: float  # parse_session_time 的 timestamp，**不是 mtime**（D23）
    size_bytes: int


def _clip_title(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:60]


def scan(root: Path) -> list[SessionMeta]:
    """扫描当前项目的会话目录，按「ID 时间」降序返回。

    三条过滤规则：

    - 目录名不是 ``YYYYMMDD-HHMMSS-xxxx`` → 跳过（D24：旧格式/手放目录不碰）
    - 目录里没有 ``conversation.jsonl`` → 跳过（空目录）
    - 行坏了 → 跳过该行，不失败

    排序只看 ID 时间，不看 mtime：mtime 会被复制、备份还原、云同步重写，
    用它排会出现「列表第一项不是启动要恢复的那个」（D25）。
    """
    base = sessions_root(root)
    if not base.is_dir():
        return []

    metas: list[SessionMeta] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        try:
            created = parse_session_time(entry.name)
        except ValueError:
            continue

        jsonl = entry / JSONL_NAME
        if not jsonl.is_file():
            continue

        try:
            size = jsonl.stat().st_size
            text = jsonl.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        meta = _scan_one(entry, jsonl, created, text, size)
        metas.append(meta)

    metas.sort(key=lambda m: (m.updated, m.session_id), reverse=True)
    return metas


def _scan_one(
    directory: Path,
    jsonl: Path,
    created: datetime,
    text: str,
    size: int,
) -> SessionMeta:
    title = ""
    model = ""
    messages = 0

    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue

        # 压缩标记：它之前的消息不再属于「当前历史」，标题与条数都要重算。
        # model 不重算——它是「这个会话最后用的模型」，跨压缩仍然有效。
        if record.get("type") == "compact":
            title = ""
            messages = 0
            continue

        if record.get("model"):
            model = str(record["model"])[:MAX_MODEL_LEN]

        role = record.get("role")
        if not role:
            continue
        messages += 1
        if not title and role == "user" and (record.get("content") or "").strip():
            title = _clip_title(str(record["content"]))

    return SessionMeta(
        session_id=directory.name,
        dir=directory,
        jsonl=jsonl,
        title=title or "（无标题）",
        messages=messages,
        model=model or UNKNOWN_MODEL,
        updated=created.timestamp(),
        size_bytes=size,
    )
