"""过期清理：判据是 ID 时间，不是 mtime。"""

import os
from datetime import datetime, timedelta
from pathlib import Path

from helpcode.memory import cleanup
from helpcode.memory import session as ses

NOW = datetime(2026, 10, 24, 12, 0, 0)


def _session(root: Path, sid: str, content: str = "x") -> Path:
    ses.ensure_session_dir(root, sid)
    (ses.jsonl_path(root, sid)).write_text(
        '{"role": "user", "content": "hi", "ts": "x", "model": "m"}\n', encoding="utf-8"
    )
    (ses.session_dir(root, sid) / "tool-results" / "a.txt").write_text(
        content, encoding="utf-8"
    )
    return ses.session_dir(root, sid)


def _sid(days_ago: int, suffix: str = "aaaa") -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y%m%d-%H%M%S") + f"-{suffix}"


def test_old_session_dir_removed(tmp_path):
    """ID 时间 31 天前 → 目录连同 tool-results 一起删（AC20）。"""
    d = _session(tmp_path, _sid(31))

    report = cleanup.run(tmp_path, now=NOW)

    assert not d.exists()
    assert report.sessions_removed == 1


def test_recent_session_kept(tmp_path):
    """ID 时间 29 天前 → 保留（AC20）。"""
    d = _session(tmp_path, _sid(29))

    report = cleanup.run(tmp_path, now=NOW)

    assert d.exists()
    assert report.sessions_removed == 0


def test_expiry_by_id_not_mtime(tmp_path):
    """反证：ID 是今天、mtime 改到 31 天前 → 必须保留。

    这条是「没有退回 mtime 判据」的唯一硬证据。只验「31 天前被删」的话，
    一个用 mtime 实现的版本也能通过。
    """
    sid = _sid(0)
    d = _session(tmp_path, sid)
    old = (NOW - timedelta(days=31)).timestamp()
    os.utime(ses.jsonl_path(tmp_path, sid), (old, old))

    report = cleanup.run(tmp_path, now=NOW)

    assert d.exists()
    assert report.sessions_removed == 0


def test_unparsable_id_skipped(tmp_path):
    """ID 无法解析 → 跳过，不删不计数（D24）。"""
    legacy = tmp_path / ".helpcode" / "sessions" / "legacy-session"
    legacy.mkdir(parents=True)
    (legacy / "conversation.jsonl").write_text("{}", encoding="utf-8")
    old = (NOW - timedelta(days=90)).timestamp()
    os.utime(legacy, (old, old))

    report = cleanup.run(tmp_path, now=NOW)

    assert legacy.exists()
    assert report.sessions_removed == 0


def test_keep_protects_target(tmp_path):
    """keep 指定的会话即使过期也不删（AC20）。"""
    sid = _sid(31)
    d = _session(tmp_path, sid)

    report = cleanup.run(tmp_path, now=NOW, keep=sid)

    assert d.exists()
    assert report.sessions_removed == 0


def test_old_tool_results_removed(tmp_path):
    """旧全局目录：31 天前的孤儿文件删除、30 天内的保留（AC40）。"""
    legacy = tmp_path / ".helpcode" / "tool-results"
    legacy.mkdir(parents=True)
    old_file = legacy / "old.txt"
    new_file = legacy / "new.txt"
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")

    old = (NOW - timedelta(days=31)).timestamp()
    os.utime(old_file, (old, old))

    report = cleanup.run(tmp_path, now=NOW)

    assert not old_file.exists()
    assert new_file.exists()
    assert report.orphan_files_removed == 1


def test_empty_legacy_dir_removed(tmp_path):
    """旧目录清空后一并 rmdir（AC40）。"""
    legacy = tmp_path / ".helpcode" / "tool-results"
    legacy.mkdir(parents=True)
    f = legacy / "old.txt"
    f.write_text("x", encoding="utf-8")
    old = (NOW - timedelta(days=31)).timestamp()
    os.utime(f, (old, old))

    cleanup.run(tmp_path, now=NOW)

    assert not legacy.exists()


def test_failure_recorded_not_raised(tmp_path, monkeypatch):
    """删除失败 → 记进 warnings、继续处理其它目录、不抛（N5）。"""
    _session(tmp_path, _sid(31, "aaaa"))
    _session(tmp_path, _sid(31, "bbbb"))

    calls = {"n": 0}
    real = cleanup.shutil.rmtree

    def flaky(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("locked")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(cleanup.shutil, "rmtree", flaky)

    report = cleanup.run(tmp_path, now=NOW)

    assert report.warnings and "删除失败" in report.warnings[0]
    assert report.sessions_removed == 1  # 第二个成功了


def test_banner_line_empty_when_clean(tmp_path):
    """没删任何东西 → banner 行是空串。"""
    _session(tmp_path, _sid(1))
    report = cleanup.run(tmp_path, now=NOW)

    assert report.banner_line() == ""


def test_banner_line_reports_count(tmp_path):
    """删了 N 个 → banner 显示 N。"""
    _session(tmp_path, _sid(31, "aaaa"))
    _session(tmp_path, _sid(40, "bbbb"))

    report = cleanup.run(tmp_path, now=NOW)

    assert report.sessions_removed == 2
    assert "已清理 2 个过期会话" == report.banner_line()


def test_no_sessions_dir_is_noop(tmp_path):
    """没有 sessions 目录 → 什么也不做、不报错、不创建目录。"""
    report = cleanup.run(tmp_path, now=NOW)

    assert report.sessions_removed == 0
    assert not (tmp_path / ".helpcode").exists()
