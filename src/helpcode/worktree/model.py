"""Worktree 子系统的数据结构。

全部 frozen：这些对象会被跨线程读（后台子 Agent 收尾时读活跃表），
可变对象一旦被谁就地改了，问题在几公里外才现形。
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Worktree:
    """一个 Worktree 的元信息。"""

    name: str  # 原始 slug（可能含 /）
    path: Path  # 绝对路径
    branch: str  # worktree-<flat>
    based_on: str  # 创建基准（HEAD 或具体 commit）
    head: str  # 创建时的 commit
    created: float = field(default_factory=time.time)
    manual: bool = False  # 用户手动创建 → 自动清理跳过


@dataclass(frozen=True)
class WorktreeSession:
    """当前活跃的隔离会话。持久化到 ``.helpcode/worktree_session.json``。"""

    original_cwd: str
    worktree_path: str
    worktree_name: str
    original_branch: str
    original_head: str
    session_id: str


class ExitAction(enum.Enum):
    """退出动作。"""

    KEEP = "keep"
    REMOVE = "remove"


@dataclass(frozen=True)
class ExitReport:
    removed: bool
    path: str
    branch: str


@dataclass(frozen=True)
class AutoCleanupReport:
    kept: bool
    path: str = ""
    branch: str = ""


@dataclass(frozen=True)
class StaleReport:
    """过期清理结果：删了哪些、因有改动保留了哪些。"""

    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.removed or self.kept)
