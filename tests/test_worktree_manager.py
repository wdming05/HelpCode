"""Worktree 生命周期：创建 / 快速恢复 / 进入退出 / 清理。

全部在临时 git 仓库里跑真 git——Worktree 的行为一半在 git 里，
mock 掉 git 等于什么都没测。
"""

import json
import os
import time
from pathlib import Path

import pytest
from git_repo import git as _git
from git_repo import make_repo

from helpcode.worktree import (
    ExitAction,
    Manager,
    NotARepoError,
    WorktreeExistsError,
    WorktreeHasChangesError,
    WorktreeNotFoundError,
)


@pytest.fixture
def repo(tmp_path):
    """一个只有一次提交的干净仓库。"""
    return make_repo(tmp_path / "repo")


def test_init_requires_repo_root(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NotARepoError):
        Manager(plain)


def test_init_rejects_subdirectory(repo):
    """仓库子目录也不能当根——Worktree 必须挂在仓库根上。"""
    sub = repo / "sub"
    sub.mkdir()
    with pytest.raises(NotARepoError):
        Manager(sub)


def test_create_makes_dir_and_branch(repo):
    m = Manager(repo)
    wt = m.create("fix-login")

    assert wt.path.is_dir()
    assert wt.path == m.worktree_dir / "fix-login"
    assert wt.branch == "worktree-fix-login"
    assert wt.head and wt.head == _git(wt.path, "rev-parse", "HEAD").strip()
    assert "worktree-fix-login" in _git(repo, "branch", "--list", "worktree-*")
    # 内容确实 checkout 出来了
    assert (wt.path / "a.txt").read_text(encoding="utf-8") == "hello\n"


def test_create_nested_slug_is_flattened(repo):
    m = Manager(repo)
    wt = m.create("feat/login")
    assert wt.path.name == "feat+login"
    assert wt.branch == "worktree-feat+login"


def test_create_duplicate_rejected(repo):
    m = Manager(repo)
    m.create("dup")
    with pytest.raises(WorktreeExistsError):
        m.create("dup")


def test_quick_restore_without_git(repo, monkeypatch):
    """目录已存在（外部创建）时走文件系统读，不调任何 git 子进程。"""
    m = Manager(repo)
    path = m.worktree_dir / "external-x"
    _git(repo, "worktree", "add", "-B", "worktree-external-x", str(path), "HEAD")

    def _boom(*args, **kwargs):
        raise AssertionError("快速恢复路径不该调用 git")

    monkeypatch.setattr("helpcode.worktree.manager.run_git", _boom)
    monkeypatch.setattr("helpcode.worktree.manager.git_text", _boom)

    wt = m.create("external-x")
    assert wt.path == path
    assert len(wt.head) == 40 and all(c in "0123456789abcdef" for c in wt.head)


def test_enter_keeps_process_cwd(repo):
    m = Manager(repo)
    m.create("enter-me")
    before = os.getcwd()
    session = m.enter("enter-me")
    assert os.getcwd() == before  # 关键：不 chdir
    assert session.worktree_path.endswith("enter-me")
    assert session.original_cwd == before
    assert session.session_id


def test_enter_writes_session(repo):
    m = Manager(repo)
    m.create("persist")
    m.enter("persist")
    raw = json.loads(m.session_file.read_text(encoding="utf-8"))
    assert raw["worktree_name"] == "persist"
    assert raw["worktree_path"].endswith("persist")


def test_enter_unknown_rejected(repo):
    m = Manager(repo)
    with pytest.raises(WorktreeNotFoundError):
        m.enter("nope")


def test_second_session_rejected(repo):
    m = Manager(repo)
    m.create("a")
    m.create("b")
    m.enter("a")
    with pytest.raises(WorktreeExistsError):
        m.enter("b")


def test_exit_keep_clears_session(repo):
    m = Manager(repo)
    m.create("keep-me")
    m.enter("keep-me")
    report = m.exit("keep-me", ExitAction.KEEP)

    assert report.removed is False
    assert m.current_session is None
    assert m.session_file.read_text(encoding="utf-8").strip() == "null"
    assert Path(report.path).is_dir()


def test_exit_remove_blocked_by_changes(repo):
    m = Manager(repo)
    wt = m.create("dirty")
    m.enter("dirty")
    (wt.path / "new.txt").write_text("x", encoding="utf-8")
    with pytest.raises(WorktreeHasChangesError):
        m.exit("dirty", ExitAction.REMOVE)
    assert wt.path.is_dir()  # 拒绝后什么都没删


def test_exit_remove_with_discard(repo):
    m = Manager(repo)
    wt = m.create("throw-away")
    m.enter("throw-away")
    (wt.path / "new.txt").write_text("x", encoding="utf-8")
    report = m.exit("throw-away", ExitAction.REMOVE, discard_changes=True)

    assert report.removed is True
    assert not Path(report.path).exists()
    assert "worktree-throw-away" not in _git(repo, "branch", "--list", "worktree-*")


def test_exit_wrong_name_rejected(repo):
    m = Manager(repo)
    m.create("cur")
    m.create("other")
    m.enter("cur")
    with pytest.raises(WorktreeNotFoundError, match="只能退出当前"):
        m.exit("other", ExitAction.KEEP)


def test_remove_non_current(repo):
    m = Manager(repo)
    m.create("cur2")
    other = m.create("other2")
    m.enter("cur2")
    report = m.remove("other2", discard_changes=True)
    assert report.removed
    assert not other.path.exists()
    assert m.current_session is not None  # 当前会话不受影响


def test_remove_blocked_by_new_commit(repo):
    """没改文件、但多了 commit 也算变更。"""
    m = Manager(repo)
    wt = m.create("committed")
    _git(wt.path, "config", "user.email", "t@example.com")
    _git(wt.path, "config", "user.name", "T")
    (wt.path / "c.txt").write_text("c", encoding="utf-8")
    _git(wt.path, "add", ".")
    _git(wt.path, "commit", "-m", "c")
    with pytest.raises(WorktreeHasChangesError):
        m.remove("committed")


def test_auto_cleanup_clean(repo):
    m = Manager(repo)
    wt = m.create("temp-clean")
    report = m.auto_cleanup("temp-clean")
    assert report.kept is False
    assert not wt.path.exists()
    assert m.get("temp-clean") is None


def test_auto_cleanup_dirty_keeps(repo):
    m = Manager(repo)
    wt = m.create("temp-dirty")
    (wt.path / "x.txt").write_text("x", encoding="utf-8")
    report = m.auto_cleanup("temp-dirty")
    assert report.kept is True
    assert report.path == str(wt.path)
    assert report.branch == wt.branch
    assert wt.path.is_dir()


def test_auto_cleanup_manual_skipped(repo):
    m = Manager(repo)
    wt = m.create("manual-one", manual=True)
    report = m.auto_cleanup("manual-one")
    assert report.kept is True
    assert wt.path.is_dir()


def _age(path: Path, hours: float) -> None:
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


def test_cleanup_stale_filters(repo):
    """三道闸：命名模式、时间、有无改动。"""
    m = Manager(repo)
    stale_clean = m.create("agent-a1234567")
    stale_dirty = m.create("agent-abcdef01")
    fresh = m.create("agent-a9999999")
    user_named = m.create("user-task")

    (stale_dirty.path / "x.txt").write_text("x", encoding="utf-8")
    for path in (stale_clean.path, stale_dirty.path, user_named.path):
        _age(path, 48)
    # fresh 保持新 mtime

    # 模拟进程重启：新实例不再把上面这些当成「本进程正在用」
    m2 = Manager(repo)
    report = m2.cleanup_stale(older_than_hours=24)

    assert "agent-a1234567" in report.removed
    assert "agent-abcdef01" in report.kept  # 有改动 → 保留
    assert "user-task" not in report.removed + report.kept  # 命名不符，不碰
    assert not stale_clean.path.exists()
    assert stale_dirty.path.is_dir()
    assert fresh.path.is_dir()
    assert user_named.path.is_dir()


def test_session_cleared_when_dir_missing(repo):
    m = Manager(repo)
    wt = m.create("gone")
    m.enter("gone")
    assert m.current_session is not None

    # 目录被外部删掉
    import shutil

    shutil.rmtree(wt.path)

    m2 = Manager(repo)
    assert m2.current_session is None
    assert json.loads(m2.session_file.read_text(encoding="utf-8")) is None


def test_scan_restores_active(repo):
    m = Manager(repo)
    m.create("scanned")
    m2 = Manager(repo)
    names = [wt.name for wt in m2.list()]
    assert "scanned" in names
    assert m2.get("scanned").head  # 从文件系统还原出了 commit
