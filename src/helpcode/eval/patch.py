"""patch 的收集与检查。

这里是 SWE-bench 评测里**最容易丢分**的一步：官方 README 给的
``git --no-pager diff HEAD`` 不含新建文件。模型新建了文件、测试也过了，
patch 里却是空的，直接判失败。所以统一走 ``git add -A`` + ``diff --cached``。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# 评测时不该进 patch 的东西：会话存档、工具结果落盘、worktree 目录
EXCLUDE_PATHS = (
    ".helpcode/sessions/",
    ".helpcode/tool-results/",
    ".helpcode/worktrees/",
)
# 缓存目录：pytest / Python 自己生成的，混进 patch 会让 diff 又长又假
EXCLUDE_DIRS = frozenset(
    {"__pycache__", ".pytest_cache", ".helpcode-eval", ".helpcode"}
)

_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_TEST_HINT = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$")


class PatchError(RuntimeError):
    """收集 patch 失败（不是 git 仓库、git 命令报错等）。"""


def _git(workspace: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workspace), "-c", "core.quotepath=false", *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise PatchError(f"git {' '.join(args)} 失败：{proc.stderr.strip()[:400]}")
    return proc.stdout


def collect_patch(workspace: Path, *, reset_index: bool = True) -> str:
    """把工作区里的改动收集成一个 unified diff。

    ``reset_index`` 先清空暂存区再全量 add——避免上一次评测残留的暂存条目
    混进这一题（同一 worktree 复用时会踩）。
    """
    if reset_index:
        _git(workspace, "reset", "--quiet")
    _git(workspace, "add", "-A")
    diff = _git(workspace, "diff", "--cached", "--text", "--no-color")
    return _filter_excluded(diff)


def _filter_excluded(diff: str) -> str:
    """按文件路径切块，丢掉不该进 patch 的块。"""
    if not diff.strip():
        return ""
    chunks = re.split(r"(?m)^(?=diff --git )", diff)
    kept: list[str] = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        match = re.search(r"^diff --git a/(.+?) b/(.+)$", chunk, re.MULTILINE)
        path = match.group(2) if match else ""
        if path.startswith(EXCLUDE_PATHS):
            continue
        if any(part in EXCLUDE_DIRS for part in path.split("/")):
            continue
        kept.append(chunk)
    return "".join(kept)


def changed_files(patch: str) -> list[str]:
    """从 diff 里取改动文件列表（取 ``+++ b/`` 行，去重保序）。"""
    seen: list[str] = []
    for path in _FILE_RE.findall(patch):
        if path == "/dev/null":
            continue
        if path not in seen:
            seen.append(path)
    return seen


def is_test_only(patch: str) -> bool:
    """patch 只动了测试文件——归因用（改测试不算修好）。"""
    files = changed_files(patch)
    return bool(files) and all(_TEST_HINT.search(f) for f in files)


def is_empty(patch: str) -> bool:
    return not patch.strip()


def apply_patch(workspace: Path, patch: str, *, three_way: bool = False) -> None:
    """在 workspace 里打补丁（评测侧打 test_patch / 校验预测 patch 时用）。"""
    if not patch.strip():
        return
    args = ["apply", "--whitespace=nowarn"]
    if three_way:
        args.append("--3way")
    proc = subprocess.run(
        ["git", "-C", str(workspace), *args, "-"],
        input=patch,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise PatchError(f"应用补丁失败：{proc.stderr.strip()[:400]}")
