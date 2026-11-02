"""创建后环境初始化：配置复制、hooks、软链、.worktreeinclude。"""

import os

import pytest
from git_repo import git, make_repo

from helpcode.worktree import Manager
from helpcode.worktree.setup import post_creation_setup


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


def test_copy_local_config(repo):
    cfg_dir = repo / ".helpcode"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("providers: []\n", encoding="utf-8")
    (cfg_dir / "settings.local.yaml").write_text("permissions: {}\n", encoding="utf-8")

    wt = Manager(repo).create("copy-cfg")
    assert (wt.path / ".helpcode" / "config.yaml").read_text(
        encoding="utf-8"
    ) == "providers: []\n"
    assert (wt.path / ".helpcode" / "settings.local.yaml").is_file()


def test_link_dependencies(repo, monkeypatch):
    """软链走 os.symlink；这里替换成记录器，避免依赖 Windows 开发者模式。"""
    (repo / "node_modules").mkdir()
    (repo / "vendor").mkdir()
    calls = []

    def fake_symlink(src, dst, target_is_directory=False):
        calls.append((str(src), str(dst), target_is_directory))
        os.mkdir(dst)  # 假装链接建成，后续断言目录存在

    monkeypatch.setattr("helpcode.worktree.setup.os.symlink", fake_symlink)
    wt = Manager(repo).create("link-deps")

    names = sorted(os.path.basename(dst) for _src, dst, _flag in calls)
    assert names == ["node_modules", "vendor"]  # .venv 不存在 → 不链
    assert all(str(p).startswith(str(wt.path)) for _s, p, _f in calls)
    assert all(flag for _s, _p, flag in calls)


def test_link_failure_only_warns(repo, monkeypatch):
    """软链失败（如无权限）不该让创建失败——只留告警。"""
    (repo / "vendor").mkdir()

    def boom(*args, **kwargs):
        raise OSError("没有权限")

    monkeypatch.setattr("helpcode.worktree.setup.os.symlink", boom)
    warnings = post_creation_setup(repo, repo / ".helpcode" / "worktrees" / "x")
    assert any("软链" in w for w in warnings)


def test_copy_included_files(repo):
    (repo / ".gitignore").write_text("*.env\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (repo / ".worktreeinclude").write_text("# 运行需要\n.env\n", encoding="utf-8")
    git(repo, "add", ".gitignore", ".worktreeinclude")
    git(repo, "commit", "-m", "add ignore")

    wt = Manager(repo).create("include-env")
    assert (wt.path / ".env").read_text(encoding="utf-8") == "SECRET=1\n"


def test_include_pattern_without_match(repo):
    (repo / ".worktreeinclude").write_text("*.nothing\n", encoding="utf-8")
    warnings = post_creation_setup(repo, repo / ".helpcode" / "worktrees" / "y")
    assert warnings == []  # 无匹配只是什么都不复制，不算告警


def test_hooks_path_configured(repo):
    (repo / ".githooks").mkdir()
    git(repo, "config", "core.hooksPath", ".githooks")

    wt = Manager(repo).create("hooks")
    got = git(wt.path, "config", "core.hooksPath").strip()
    assert got.replace("\\", "/").endswith(".githooks")
    assert os.path.isabs(got)


def test_all_sources_missing_ok(repo):
    """一个源文件都没有时，创建照样成功（初始化全是 best-effort）。"""
    wt = Manager(repo).create("bare")
    assert wt.path.is_dir()
    assert not (wt.path / ".helpcode" / "config.yaml").exists()
