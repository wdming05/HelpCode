"""git 子进程封装 + 纯文件系统的 ref 读取。

两件事：
1. 所有 git 调用都禁交互（``GIT_TERMINAL_PROMPT=0`` / ``GIT_ASKPASS`` 置空），
   自动化场景绝不能停在密码提示上。
2. 快速恢复走文件系统读（``worktree_head_from_fs``），不启动任何子进程——
   目录已存在说明 worktree 注册还在，直接读 ``.git`` 指针就够了。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# 一次性构造：每个 git 调用都带上，避免各处重复拼环境变量。
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}

DEFAULT_GIT_TIMEOUT = 60.0


def run_git(
    args: list[str],
    cwd: str | Path,
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """执行一条 git 命令，**不抛异常**（超时/找不到 git 也包成结果）。"""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=GIT_ENV,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=-1, stdout="", stderr=str(e)
        )


def git_text(args: list[str], cwd: str | Path) -> str | None:
    """成功返回 stdout.strip()，失败返回 None（调用方按「不知道」处理）。"""
    proc = run_git(args, cwd)
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def _norm(path: str | Path) -> str:
    """归一化路径用于比较：统一分隔符、消掉末尾斜杠、Windows 下忽略大小写。"""
    text = str(path).replace("\\", "/").rstrip("/")
    return os.path.normcase(os.path.normpath(text))


def is_repo_root(path: str | Path) -> bool:
    """判断 path 是否**就是**仓库根（而不是仓库里的子目录）。"""
    top = git_text(["rev-parse", "--show-toplevel"], path)
    if not top:
        return False
    return _norm(top) == _norm(Path(path).resolve())


def head_of(cwd: str | Path) -> str:
    return git_text(["rev-parse", "HEAD"], cwd) or ""


def branch_of(cwd: str | Path) -> str:
    return git_text(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _main_git_dir(gitdir: Path) -> Path:
    """worktree 的 gitdir 形如 ``<repo>/.git/worktrees/<flat>``；主 .git 靠 commondir 找。"""
    common = _read_text(gitdir / "commondir")
    if common:
        return (gitdir / common).resolve()
    # 没有 commondir 说明它是主工作树本身的 .git 目录
    return gitdir


def _lookup_ref(main_git: Path, ref: str) -> str:
    """在 refs/ 或 packed-refs 里找目标 ref 的 sha。找不到返回空串。"""
    loose = main_git / ref
    if loose.is_file():
        return _read_text(loose)
    packed = main_git / "packed-refs"
    if packed.is_file():
        for line in _read_text(packed).splitlines():
            if line.startswith(("#", "^")):
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip() == ref:
                return parts[0].strip()
    return ""


def worktree_head_from_fs(wt_path: str | Path) -> str:
    """只读文件系统还原 worktree 的 HEAD commit；读不到返回空串。

    路径：``<wt>/.git``（文件，含 ``gitdir:``）→ ``<gitdir>/HEAD`` →
    ``refs/heads/<branch>`` 或 ``packed-refs``。全程不调 git。
    """
    wt = Path(wt_path)
    pointer = _read_text(wt / ".git")
    if not pointer.startswith("gitdir:"):
        return ""
    raw = pointer[len("gitdir:") :].strip()
    gitdir = Path(raw) if os.path.isabs(raw) else (wt / raw)
    gitdir = gitdir.resolve()

    head = _read_text(gitdir / "HEAD")
    if not head:
        return ""
    if not head.startswith("ref:"):
        return head  # detached：HEAD 里直接就是 sha

    ref = head[len("ref:") :].strip()
    sha = _lookup_ref(_main_git_dir(gitdir), ref)
    if sha:
        return sha
    return _lookup_ref(gitdir, ref)


def ignored_files(repo_root: str | Path) -> list[str]:
    """列出被 .gitignore 忽略的文件（相对仓库根的 posix 路径）。

    不带 ``--directory``：要逐个复制文件，列出目录还得再展开一层。
    """
    proc = run_git(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        repo_root,
    )
    if proc.returncode != 0 or not proc.stdout:
        return []
    return [p for p in proc.stdout.split("\0") if p]
