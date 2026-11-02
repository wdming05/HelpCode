"""后台子任务管理：状态、结果、停止、完成回调。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..permission import Mode

logger = logging.getLogger(__name__)


@dataclass
class BackgroundTask:
    """一个后台子 Agent 任务。"""

    id: str
    name: str
    agent: object
    conv: object
    task: str
    mode: Mode = Mode.DEFAULT
    status: str = "running"  # running / completed / failed / cancelled
    result: str = ""
    error: str | None = None
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    future: asyncio.Task | None = None
    # ch14：收尾钩子。在结果写入之后、完成通知之前调用，
    # 用来做 Worktree 清理并把「已保留」信息补进 result。
    finalize: object | None = None

    @property
    def elapsed(self) -> float:
        return (self.end_time or time.time()) - self.start_time


class TaskManager:
    """内存中的后台任务表。进程退出即丢，不做跨会话持久化。"""

    def __init__(self, on_done=None) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._order: list[str] = []
        self._on_done = on_done
        self._n = 0

    def launch(
        self,
        agent,
        conv,
        task: str,
        *,
        name: str = "",
        mode: Mode = Mode.DEFAULT,
        finalize=None,
    ) -> BackgroundTask:
        self._n += 1
        bt = BackgroundTask(
            id=f"task-{self._n}",
            name=name,
            agent=agent,
            conv=conv,
            task=task,
            mode=mode,
            finalize=finalize,
        )
        self._tasks[bt.id] = bt
        self._order.append(bt.id)
        bt.future = asyncio.create_task(self._run(bt))
        return bt

    async def _run(self, bt: BackgroundTask) -> None:
        try:
            bt.result = await bt.agent.run_to_completion(bt.conv, bt.task, bt.mode)
            bt.status = "completed"
        except asyncio.CancelledError:
            bt.status = "cancelled"
            raise
        except Exception as e:  # 子 Agent 失败只记状态，不冒出后台任务
            bt.status = "failed"
            bt.error = f"{e.__class__.__name__}: {e}"
        finally:
            bt.end_time = time.time()
            # finalize 必须在通知之前：通知里带的 result 要已经含「Worktree 保留在 …」
            if bt.finalize is not None:
                try:
                    bt.finalize(bt)
                except Exception:
                    logger.warning("任务收尾钩子失败（已忽略）", exc_info=True)
            if self._on_done is not None:
                try:
                    self._on_done(bt)
                except Exception:
                    logger.warning("任务完成回调失败（已忽略）", exc_info=True)

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list(self) -> list[BackgroundTask]:
        return [self._tasks[i] for i in self._order]

    def stop(self, task_id: str) -> bool:
        bt = self._tasks.get(task_id)
        if bt is None or bt.future is None or bt.future.done():
            return False
        bt.future.cancel()
        return True

    async def drain(self, timeout: float = 5.0) -> None:
        """等所有未完成任务收尾（测试/退出用）。"""
        pending = [t.future for t in self.list() if t.future and not t.future.done()]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout
                )
            except TimeoutError:
                pass
