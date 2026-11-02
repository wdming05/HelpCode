"""落盘原语：锁互斥、过期锁回收、原子写、并发不丢数据。"""

import json
import os
import threading
import time

import pytest

from helpcode.team.state import (
    FileLock,
    LockTimeoutError,
    locked_json,
    read_json,
    write_json,
)


def test_lock_excludes(tmp_path):
    path = tmp_path / "a.lock"
    first = FileLock(path, timeout=5.0, retries=2)
    first.acquire()
    try:
        with pytest.raises(LockTimeoutError):
            FileLock(path, timeout=5.0, retries=2).acquire()
    finally:
        first.release()
    # 释放后能再拿到
    with FileLock(path, timeout=5.0, retries=2):
        assert path.exists()
    assert not path.exists()


def test_stale_lock_reclaimed(tmp_path):
    path = tmp_path / "stale.lock"
    path.write_text("", encoding="utf-8")
    old = time.time() - 100
    os.utime(path, (old, old))

    with FileLock(path, timeout=1.0, retries=3):
        pass
    assert not path.exists()


def test_concurrent_writes_keep_all(tmp_path):
    path = tmp_path / "counter.json"

    def worker():
        for _ in range(5):
            with locked_json(path) as data:
                data["n"] = int(data.get("n") or 0) + 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert read_json(path)["n"] == 20


def test_write_json_atomic(tmp_path):
    path = tmp_path / "x.json"
    write_json(path, {"a": 1})
    write_json(path, {"a": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}
    # 临时文件不能残留
    assert not (tmp_path / "x.json.tmp").exists()


def test_read_json_tolerates_garbage(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not json", encoding="utf-8")
    assert read_json(path) == {}
    assert read_json(tmp_path / "missing.json", {"k": 1}) == {"k": 1}
