"""Worktree 生命周期主控：活跃表、会话持久化、变更检测、清理。

一条铁律：本模块**从不调用 os.chdir 改变进程工作目录**（唯一例外是退出时
切回原始目录的兜底）。工作目录由上层显式传给工具调用——否则主 Agent 与后台
子 Agent 会在同一个进程级 cwd 上互相踩。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from .git import (
    branch_of,
    git_text,
    head_of,
    is_repo_root,
    run_git,
    worktree_head_from_fs,
)
from .model import (
    AutoCleanupReport,
    ExitAction,
    ExitReport,
    StaleReport,
    Worktree,
    WorktreeSession,
)
from .setup import post_creation_setup
from .slug import branch_for, flat_slug, validate_slug

logger = logging.getLogger(__name__)

WORKTREE_DIRNAME = Path(".helpcode") / "worktrees"
SESSION_FILENAME = Path(".helpcode") / "worktree_session.json"

# 只有这种命名才是后台临时 Worktree（ch13 的隔离子 Agent 用 agent-aXXXXXXX）
STALE_RE = re.compile(r"^agent-a[0-9a-f]{7}$")
DEFAULT_STALE_HOURS = 24.0


class NotARepoError(RuntimeError):
    """给定目录不是 git 仓库根，Worktree 功能不可用。"""


class WorktreeExistsError(RuntimeError):
    """同名 Worktree 已存在，或已有活跃会话。"""


class WorktreeNotFoundError(RuntimeError):
    """Worktree 或会话不存在。"""


class WorktreeHasChangesError(RuntimeError):
    """有未提交修改 / 未合并 commit，拒绝删除。"""


class Manager:
    """一个仓库内的 Worktree 管理器。线程安全（单把 RLock）。"""

    def __init__(
        self, repo_root: str | Path, *, worktree_dir: Path | None = None
    ) -> None:
        root = Path(repo_root).resolve()
        if not is_repo_root(root):
            raise NotARepoError(f"{root} 不是 git 仓库根目录")

        self.repo_root = root
        self.worktree_dir = (worktree_dir or (root / WORKTREE_DIRNAME)).resolve()
        self.session_file = root / SESSION_FILENAME
        # RLock：auto_cleanup 会调到 remove，用普通 Lock 就会自锁死
        self._lock = threading.RLock()
        self._active: dict[str, Worktree] = {}
        self._session: WorktreeSession | None = None
        # 本进程创建、尚未删除的名字。过期清理要避开它们——正在跑的子 Agent
        # 的文件改动可能还没落盘，按「可能正在使用」处理最安全。
        # 注意不能用「在活跃表里」当判据：启动扫描会把所有历史目录都填进活跃表。
        self._busy: set[str] = set()

        try:
            self.worktree_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise NotARepoError(
                f"无法创建 Worktree 目录 {self.worktree_dir}：{e}"
            ) from e

        self._active = self._scan_active()
        self._session = self._load_session()
        if self._session is not None and not Path(self._session.worktree_path).is_dir():
            # 目录没了说明会话已失效：清空文件，否则下次启动会「恢复」到不存在的路径
            logger.info("Worktree 会话指向的目录已不存在，已清空会话文件")
            self._session = None
            self._save_session(None)

    # ---------------------------------------------------------------- 只读

    @property
    def current_session(self) -> WorktreeSession | None:
        return self._session

    def list(self) -> list[Worktree]:
        with self._lock:
            return list(self._active.values())

    def get(self, name: str) -> Worktree | None:
        with self._lock:
            return self._active.get(name)

    # ---------------------------------------------------------------- 创建

    def create(
        self, name: str, base_ref: str = "HEAD", *, manual: bool = False
    ) -> Worktree:
        slug = validate_slug(name)
        flat = flat_slug(slug)
        path = self.worktree_dir / flat
        branch = branch_for(slug)

        with self._lock:
            if slug in self._active:
                raise WorktreeExistsError(f"Worktree 已存在：{slug}")
            # 不同 slug 可能扁平化到同一个目录（a/b 与 a+b），也要拦住
            if any(wt.path == path for wt in self._active.values()):
                raise WorktreeExistsError(f"目录已被其它 Worktree 占用：{path}")

            if path.is_dir():
                # 快速恢复：worker 目录还在，注册也还在，读文件即可
                existing = Worktree(
                    name=slug,
                    path=path,
                    branch=branch,
                    based_on=base_ref,
                    head=worktree_head_from_fs(path),
                    manual=manual,
                )
                self._active[slug] = existing
                logger.info("快速恢复已有 Worktree：%s", path)
                return existing

            proc = run_git(
                ["worktree", "add", "-B", branch, str(path), base_ref], self.repo_root
            )
            if proc.returncode != 0:
                shutil.rmtree(path, ignore_errors=True)
                detail = (proc.stderr or proc.stdout or "").strip()[:200]
                raise RuntimeError(f"git worktree add 失败：{detail}")

            for warning in post_creation_setup(self.repo_root, path):
                logger.warning("Worktree %s 初始化告警：%s", slug, warning)

            wt = Worktree(
                name=slug,
                path=path,
                branch=branch,
                based_on=base_ref,
                head=head_of(path) or worktree_head_from_fs(path),
                manual=manual,
            )
            self._active[slug] = wt
            self._busy.add(slug)
            return wt

    # ---------------------------------------------------------------- 会话

    def enter(self, name: str) -> WorktreeSession:
        with self._lock:
            wt = self._active.get(name)
            if wt is None:
                raise WorktreeNotFoundError(f"Worktree 不存在：{name}")
            if self._session is not None and self._session.worktree_name != name:
                raise WorktreeExistsError(
                    f"已有活跃会话 {self._session.worktree_name}，请先退出"
                )
            session = WorktreeSession(
                original_cwd=os.getcwd(),
                worktree_path=str(wt.path),
                worktree_name=name,
                original_branch=branch_of(self.repo_root),
                original_head=head_of(self.repo_root),
                session_id=uuid.uuid4().hex,
            )
            self._session = session
            self._save_session(session)
            return session

    def exit(
        self,
        name: str,
        action: ExitAction = ExitAction.KEEP,
        *,
        discard_changes: bool = False,
    ) -> ExitReport:
        with self._lock:
            wt = self._active.get(name)
            if wt is None:
                raise WorktreeNotFoundError(f"Worktree 不存在：{name}")
            session = self._session
            if session is None or session.worktree_name != name:
                raise WorktreeNotFoundError(
                    f"只能退出当前 Worktree 会话（当前为 {name}）"
                )

            if (
                action is ExitAction.REMOVE
                and not discard_changes
                and self._has_changes(wt.path, wt.head)
            ):
                raise WorktreeHasChangesError(
                    "Worktree 有未提交修改或新增 commit，拒绝删除"
                )

            # 兜底：本章不 chdir，但历史会话/外部工具可能留下残留，切回原目录成本为零
            try:
                os.chdir(session.original_cwd)
            except OSError:
                logger.warning("切回原始目录失败：%s", session.original_cwd)

            self._session = None
            self._save_session(None)

            if action is ExitAction.REMOVE:
                return self._remove_locked(name)
            return ExitReport(removed=False, path=str(wt.path), branch=wt.branch)

    def remove(self, name: str, *, discard_changes: bool = False) -> ExitReport:
        with self._lock:
            wt = self._active.get(name)
            if wt is None:
                raise WorktreeNotFoundError(f"Worktree 不存在：{name}")
            if not discard_changes and self._has_changes(wt.path, wt.head):
                raise WorktreeHasChangesError(
                    "Worktree 有未提交修改或新增 commit，拒绝删除"
                )
            if self._session is not None and self._session.worktree_name == name:
                try:
                    os.chdir(self._session.original_cwd)
                except OSError:
                    pass
                self._session = None
                self._save_session(None)
            return self._remove_locked(name)

    # ---------------------------------------------------------------- 清理

    def auto_cleanup(self, name: str) -> AutoCleanupReport:
        """子 Agent 收尾用：无变更删掉，有变更保留并回报。"""
        with self._lock:
            wt = self._active.get(name)
            if wt is None:
                raise WorktreeNotFoundError(f"Worktree 不存在：{name}")
            if wt.manual:
                return AutoCleanupReport(kept=True, path=str(wt.path), branch=wt.branch)
            if self._has_changes(wt.path, wt.head):
                return AutoCleanupReport(kept=True, path=str(wt.path), branch=wt.branch)
            self._remove_locked(name)
            return AutoCleanupReport(kept=False, path=str(wt.path), branch=wt.branch)

    def cleanup_stale(
        self, older_than_hours: float = DEFAULT_STALE_HOURS
    ) -> StaleReport:
        """扫掉过期的后台临时 Worktree：命名模式 + 时间 + 变更三道闸。"""
        report = StaleReport()
        cutoff = time.time() - older_than_hours * 3600
        with self._lock:
            try:
                children = sorted(self.worktree_dir.iterdir())
            except OSError:
                return report
            for child in children:
                if not child.is_dir() or not STALE_RE.match(child.name):
                    continue
                if child.name.replace("+", "/") in self._busy:
                    continue  # 本进程创建且还没删 = 可能正在跑，别动
                try:
                    if child.stat().st_mtime >= cutoff:
                        continue
                except OSError:
                    continue
                base = worktree_head_from_fs(child)
                if self._has_changes(child, base):
                    report.kept.append(child.name)
                    continue
                shutil.rmtree(child, ignore_errors=True)
                report.removed.append(child.name)
            if report.removed:
                run_git(["worktree", "prune"], self.repo_root)
        return report

    # ---------------------------------------------------------------- 内部

    def _remove_locked(self, name: str) -> ExitReport:
        wt = self._active.pop(name, None)
        if wt is None:
            raise WorktreeNotFoundError(f"Worktree 不存在：{name}")
        self._busy.discard(name)
        run_git(["worktree", "remove", "--force", str(wt.path)], self.repo_root)
        # Windows 上目录句柄释放有延迟，紧接着删分支常报「分支已检出」
        time.sleep(0.1)
        run_git(["branch", "-D", wt.branch], self.repo_root)
        shutil.rmtree(wt.path, ignore_errors=True)  # 兜底：git 没清干净的残留
        return ExitReport(removed=True, path=str(wt.path), branch=wt.branch)

    def _has_changes(self, path: Path, base: str) -> bool:
        """有未提交修改 / 有新增 commit 即为 True；命令失败也 True（fail-closed）。"""
        proc = run_git(["status", "--porcelain"], path)
        if proc.returncode != 0:
            return True
        if (proc.stdout or "").strip():
            return True
        if not base:
            return True
        count = git_text(["rev-list", "--count", f"{base}..HEAD"], path)
        if count is None:
            return True
        try:
            return int(count) > 0
        except ValueError:
            return True

    def _scan_active(self) -> dict[str, Worktree]:
        """从目录还原活跃表；名字里的 + 还原成 /。

        扫出来的都标成 manual=True：它们要么是用户手动建的，要么是上次进程
        遗留的，自动清理不该对「来历不明」的目录动手。
        """
        active: dict[str, Worktree] = {}
        try:
            children = sorted(self.worktree_dir.iterdir())
        except OSError:
            return active
        for child in children:
            if not child.is_dir():
                continue
            name = child.name.replace("+", "/")
            active[name] = Worktree(
                name=name,
                path=child,
                branch=branch_for(name),
                based_on="",
                head=worktree_head_from_fs(child),
                manual=True,
            )
        return active

    def _load_session(self) -> WorktreeSession | None:
        if not self.session_file.is_file():
            return None
        try:
            raw = json.loads(self.session_file.read_text(encoding="utf-8") or "null")
        except (OSError, json.JSONDecodeError):
            logger.warning("Worktree 会话文件损坏，按无会话处理")
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return WorktreeSession(
                original_cwd=str(raw["original_cwd"]),
                worktree_path=str(raw["worktree_path"]),
                worktree_name=str(raw["worktree_name"]),
                original_branch=str(raw.get("original_branch", "")),
                original_head=str(raw.get("original_head", "")),
                session_id=str(raw.get("session_id", "")),
            )
        except KeyError:
            return None

    def _save_session(self, session: WorktreeSession | None) -> None:
        """原子写：先写 .tmp 再 replace。写 None 时落 ``null``（不删文件）。"""
        payload = (
            "null"
            if session is None
            else json.dumps(
                {
                    "original_cwd": session.original_cwd,
                    "worktree_path": session.worktree_path,
                    "worktree_name": session.worktree_name,
                    "original_branch": session.original_branch,
                    "original_head": session.original_head,
                    "session_id": session.session_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.session_file.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self.session_file)
        except OSError as e:
            logger.warning("写 Worktree 会话文件失败：%s", e)
