"""测试用临时 git 仓库工装。

Worktree 的行为一半在 git 里：mock 掉 git 就没有测试价值了，所以测试一律
在 tmp_path 里 `git init` 一个真仓库。这里集中两件事：
1. 用仓库级 user.email / user.name，避免依赖开发机的全局配置；
2. 首次提交固定有一个文件，方便断言 checkout 内容。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(cwd, *args: str) -> str:
    """跑一条 git 命令，失败即测试失败。"""
    proc = subprocess.run(
        ["git", *args],
        check=False,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
    )
    assert proc.returncode == 0, f"git {' '.join(args)} 失败：{proc.stderr}"
    return proc.stdout


def make_repo(path: Path, *, filename: str = "a.txt", content: str = "hello\n") -> Path:
    """在 path 下建一个只有一次提交的仓库，返回仓库根。"""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "T")
    (path / filename).write_text(content, encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "init")
    return path
