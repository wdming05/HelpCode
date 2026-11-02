"""Hook 引擎：按事件分派、条件匹配、执行控制（only_once / async / timeout）。"""

from __future__ import annotations

import asyncio
import logging

from .actions import run_action
from .events import HookEvent
from .schema import Hook, HookResult

logger = logging.getLogger(__name__)


class HookEngine:
    """持有全部 Hook，按事件触发。

    ``runner`` 可注入（测试用）；默认是真正的 ``run_action``。
    """

    def __init__(self, hooks: list[Hook], runner=None) -> None:
        self._hooks = list(hooks)
        self._by_event: dict[HookEvent, list[Hook]] = {}
        for hook in self._hooks:
            self._by_event.setdefault(hook.event, []).append(hook)
        self._runner = runner if runner is not None else run_action
        self._fired: set[str] = set()  # only_once 的会话内去重
        self._bg: set[asyncio.Task] = set()

    def reset_session(self) -> None:
        """新会话开始：清空 only_once 记录。"""
        self._fired.clear()

    async def dispatch(self, event: HookEvent, payload: dict) -> HookResult:
        """触发某事件的全部 Hook，返回汇总结果。

        **永不抛异常**：任何一条 Hook 出错都只记日志（N1）。
        """
        result = HookResult()
        for hook in self._by_event.get(event, []):
            try:
                if hook.condition is not None and not hook.condition.matches(payload):
                    continue
                if hook.only_once and hook.name in self._fired:
                    continue
                if hook.only_once:
                    self._fired.add(hook.name)
                if hook.is_async:
                    task = asyncio.create_task(self._runner(hook, payload))
                    self._bg.add(task)
                    task.add_done_callback(self._bg.discard)
                    continue
                sub = await self._runner(hook, payload)
                if sub.block_reason and result.block_reason is None:
                    result.block_reason = sub.block_reason
                result.injected.extend(sub.injected)
            except Exception:
                logger.warning("Hook「%s」分派失败（已忽略）", hook.name, exc_info=True)
        return result

    async def drain(self) -> None:
        """等后台 async 任务收尾（测试/退出用）。"""
        pending = [t for t in self._bg if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
