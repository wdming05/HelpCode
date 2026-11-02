"""Worktree 创建后的环境初始化。

新建出来的工作目录是「干净 checkout」：本地配置没有、git hooks 路径没继承、
大依赖目录要重新装、被忽略但运行必需的文件（.env 之类）也不在。四步补齐，
全部 best-effort——任何一步失败只告警，绝不因此让创建失败。
"""

from __future__ import annotations

import logging
import os
import shutil
from fnmatch import fnmatch
from pathlib import Path

from .git import git_text, ignored_files, run_git

logger = logging.getLogger(__name__)

# 体积大、重装代价高、且内容与主仓库一致的目录——软链过来最省事
DEFAULT_LINK_DIRS = ("node_modules", ".venv", "vendor")

# 含本地密钥的配置：不复制的话 Worktree 里的会话没有 provider / 没有本地权限规则
LOCAL_CONFIG_FILES = (".helpcode/config.yaml", ".helpcode/settings.local.yaml")

INCLUDE_FILE = ".worktreeinclude"

# 这些目录本身是本地运行数据，被 .gitignore 忽略后会出现在 ignored_files 里；
# 不排除的话，模式命中时会把自己生成的 Worktree 内容再复制一遍。
_SKIP_PREFIXES = (
    ".helpcode/worktrees/",
    ".helpcode/sessions/",
    ".helpcode/tool-results/",
)


def _copy_local_config(repo_root: Path, wt_path: Path) -> None:
    """A：复制本地配置文件到 Worktree 同位置。"""
    for rel in LOCAL_CONFIG_FILES:
        src = repo_root / rel
        if not src.is_file():
            continue
        dst = wt_path / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _configure_hooks(repo_root: Path, wt_path: Path) -> None:
    """B：把 hooksPath 配成与主仓库一致（git 不随 worktree 继承这个配置）。"""
    hooks = git_text(["config", "core.hooksPath"], repo_root)
    if hooks:
        target = Path(hooks)
        if not target.is_absolute():
            target = (repo_root / target).resolve()
    elif (repo_root / ".husky").is_dir():
        target = (repo_root / ".husky").resolve()
    else:
        return
    if not target.exists():
        return
    run_git(["config", "core.hooksPath", str(target)], wt_path)


def _link_dependencies(repo_root: Path, wt_path: Path) -> None:
    """C：软链大依赖目录；Windows 无权限时抛错，由上层记成告警。"""
    for name in DEFAULT_LINK_DIRS:
        src = repo_root / name
        dst = wt_path / name
        if not src.is_dir() or dst.exists() or dst.is_symlink():
            continue
        os.symlink(src, dst, target_is_directory=True)


def _match_include(rel: str, patterns: list[str]) -> bool:
    """匹配规则：模式含 ``/`` 时按相对路径匹配，否则按文件名匹配。

    与 .gitignore 的直觉一致——写 ``.env`` 就该命中所有层级的 .env。
    """
    base = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        if fnmatch(rel, pat) or ("/" not in pat and fnmatch(base, pat)):
            return True
    return False


def _read_patterns(repo_root: Path) -> list[str]:
    path = repo_root / INCLUDE_FILE
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        out.append(text)
    return out


def _copy_included(repo_root: Path, wt_path: Path) -> None:
    """D：按 .worktreeinclude 补上被忽略但运行需要的文件。"""
    patterns = _read_patterns(repo_root)
    if not patterns:
        return
    for rel in ignored_files(repo_root):
        if rel.startswith(_SKIP_PREFIXES):
            continue
        if not _match_include(rel, patterns):
            continue
        src = repo_root / rel
        if not src.is_file():
            continue  # 目录/失效软链跳过
        dst = wt_path / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


_STEPS = (
    ("复制本地配置", _copy_local_config),
    ("配置 git hooks", _configure_hooks),
    ("软链依赖目录", _link_dependencies),
    ("复制 .worktreeinclude 命中的文件", _copy_included),
)


def post_creation_setup(repo_root: Path, wt_path: Path) -> list[str]:
    """跑完四步初始化，返回告警列表（不抛异常）。"""
    warnings: list[str] = []
    for label, step in _STEPS:
        try:
            step(repo_root, wt_path)
        except Exception as e:  # 初始化失败不该让创建失败（N3）
            msg = f"{label}失败（{e.__class__.__name__}: {e}）"
            logger.warning("Worktree %s %s", wt_path, msg)
            warnings.append(msg)
    return warnings
