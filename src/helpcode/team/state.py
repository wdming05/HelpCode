"""团队落盘原语：锁文件 + 原子写 + 「加锁读改写」上下文。

为什么不用 ``threading.Lock``：窗格后端的队员是**独立进程**，线程锁管不到。
所以用一个独占创建的锁文件做跨进程互斥；写入统一走「临时文件 + 替换」，
进程被杀也不会留下半截 JSON。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

logger = logging.getLogger(__name__)

# 锁最长持有时间：超过就认为持锁进程已经死了（正常写一个 JSON 远小于 10s）
DEFAULT_LOCK_TIMEOUT = 10.0
# 抢不到锁的重试次数与抖动区间——抖动是为了避免两个进程同节奏互相撞
DEFAULT_RETRIES = 10
JITTER_RANGE = (0.005, 0.1)


class LockTimeoutError(RuntimeError):
    """抢锁重试用尽。调用方应降级为「告警 + 跳过本次写入」。"""


class FileLock:
    """独占锁文件（``O_CREAT | O_EXCL``）。

    抢不到就等一会儿再试；如果现有锁文件的 mtime 已经超过 ``timeout``，
    说明持锁方已经死了，直接清掉重来——否则一次崩溃会永久锁死目录。
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = DEFAULT_LOCK_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.retries = retries
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(self.retries):
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return
            except FileExistsError:
                if self._is_stale():
                    self._clear_stale()
                    continue
                time.sleep(random.uniform(*JITTER_RANGE))
        raise LockTimeoutError(f"抢锁超时：{self.path}")

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass

    def _is_stale(self) -> bool:
        try:
            return (time.time() - self.path.stat().st_mtime) > self.timeout
        except OSError:
            return False

    def _clear_stale(self) -> None:
        logger.warning("清理过期锁文件：%s", self.path)
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def write_json(path: Path, data) -> None:
    """原子写：先写 ``.tmp`` 再 ``os.replace``（同目录 rename 在 Windows 也可替换）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: dict | None = None) -> dict:
    """读 JSON；文件不存在或损坏都返回 ``default``（默认空 dict）。

    损坏时只告警不抛：一个坏文件不该让整个团队功能失效。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return dict(default or {})
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        logger.warning("JSON 文件损坏，按空处理：%s", path)
        return dict(default or {})
    return data if isinstance(data, dict) else dict(default or {})


@contextmanager
def locked_json(path: Path) -> Iterator[dict]:
    """加锁 → 读出 dict → 交给调用方改 → 解锁前原子写回。

    邮箱与任务表共用这一条路径：并发安全只有一份实现，改起来不会漏。
    """
    path = Path(path)
    lock = FileLock(path.with_name(path.name + ".lock"))
    lock.acquire()
    try:
        data = read_json(path)
        yield data
        write_json(path, data)
    finally:
        lock.release()
