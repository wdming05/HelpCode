"""Worktree 文件隔离：同一仓库多工作目录，互不覆盖。

对外只暴露生命周期主控与异常；数据结构从 ``.model`` 取。
"""

from .manager import (
    Manager,
    NotARepoError,
    WorktreeExistsError,
    WorktreeHasChangesError,
    WorktreeNotFoundError,
)
from .model import (
    AutoCleanupReport,
    ExitAction,
    ExitReport,
    StaleReport,
    Worktree,
    WorktreeSession,
)
from .notice import build_worktree_notice
from .slug import SlugError, branch_for, flat_slug, validate_slug

__all__ = [
    "AutoCleanupReport",
    "ExitAction",
    "ExitReport",
    "Manager",
    "NotARepoError",
    "SlugError",
    "StaleReport",
    "Worktree",
    "WorktreeExistsError",
    "WorktreeHasChangesError",
    "WorktreeNotFoundError",
    "WorktreeSession",
    "branch_for",
    "build_worktree_notice",
    "flat_slug",
    "validate_slug",
]
