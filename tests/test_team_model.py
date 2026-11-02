"""团队数据结构：名字规范化、花名册、配置读写、Manager。"""

import json

import pytest
from git_repo import make_repo

from helpcode.team import Manager, Team, TeamError, TeammateInfo, sanitize_name
from helpcode.worktree import Manager as WorktreeManager


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


@pytest.fixture
def mgr(repo, tmp_path):
    return Manager(tmp_path / "home", WorktreeManager(repo))


def test_sanitize_name():
    assert sanitize_name("refactor-2026") == "refactor-2026"
    # 中文保留（Windows 路径支持），空格换成连字符
    assert sanitize_name("重构 模块") == "重构-模块"
    assert sanitize_name("a b/c") == "a-b-c"
    assert sanitize_name("--x--") == "x"
    assert sanitize_name("a   b") == "a-b"


def test_sanitize_rejects_empty():
    with pytest.raises(TeamError):
        sanitize_name("   ")
    with pytest.raises(TeamError):
        sanitize_name("///")


def test_sanitize_rejects_reserved():
    for name in (".", ".."):
        with pytest.raises(TeamError):
            sanitize_name(name)


def test_create_team_writes_config(mgr):
    team = mgr.create("refactor")
    assert team.config_path.is_file()
    raw = json.loads(team.config_path.read_text(encoding="utf-8"))
    assert raw["sanitized_name"] == "refactor"
    assert raw["backend"] in ("tmux", "iterm2", "in-process")
    assert raw["members"][0]["name"] == "lead"


def test_name_conflict_suffix(mgr):
    first = mgr.create("dup")
    second = mgr.create("dup")
    third = mgr.create("dup")
    assert first.sanitized_name == "dup"
    assert second.sanitized_name == "dup-2"
    assert third.sanitized_name == "dup-3"


def test_get_by_raw_and_sanitized(mgr):
    team = mgr.create("feat x")
    assert mgr.get("feat x") is team
    assert mgr.get("feat-x") is team
    assert mgr.get("nope") is None


def test_add_member_persists(mgr):
    team = mgr.create("persist")
    team.add_member(TeammateInfo(name="alice", agent_id="task-1"))
    reloaded = Team.load(team.config_path)
    assert reloaded.member("alice") is not None
    assert reloaded.member("alice").agent_id == "task-1"


def test_duplicate_member_rejected(mgr):
    team = mgr.create("dup-member")
    team.add_member(TeammateInfo(name="bob", agent_id="task-2"))
    with pytest.raises(TeamError):
        team.add_member(TeammateInfo(name="bob", agent_id="task-3"))


def test_set_member_active_persists(mgr):
    team = mgr.create("active")
    team.add_member(TeammateInfo(name="carol", agent_id="task-4", is_active=True))
    team.set_member_active("carol", False)
    assert Team.load(team.config_path).member("carol").is_active is False
    # 其它字段不能被覆盖掉
    assert Team.load(team.config_path).member("carol").agent_id == "task-4"


def test_remove_member_persists(mgr):
    team = mgr.create("remove")
    team.add_member(TeammateInfo(name="dave", agent_id="task-5"))
    team.remove_member("dave")
    assert Team.load(team.config_path).member("dave") is None


def test_broken_config_skipped(repo, tmp_path):
    home = tmp_path / "home"
    good = Manager(home, WorktreeManager(repo))
    good.create("good")
    bad_dir = home / ".helpcode" / "teams" / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "config.json").write_text("{ not json", encoding="utf-8")

    again = Manager(home, WorktreeManager(repo))
    names = [t.sanitized_name for t in again.list()]
    assert "good" in names
    assert "bad" not in names


def test_scan_restores_teams(repo, tmp_path):
    home = tmp_path / "home"
    Manager(home, WorktreeManager(repo)).create("scanned")
    again = Manager(home, WorktreeManager(repo))
    assert again.get("scanned") is not None
