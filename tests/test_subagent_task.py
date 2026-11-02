"""后台任务管理。"""

import asyncio

from helpcode.permission import Mode
from helpcode.subagent.task import TaskManager
from helpcode.subagent.tools import TaskGetTool, TaskListTool, TaskStopTool


class _FakeAgent:
    def __init__(self, result="done", fail=False, delay=0.0):
        self.result = result
        self.fail = fail
        self.delay = delay

    async def run_to_completion(self, conv, task, mode=Mode.DEFAULT):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return self.result


def _run(coro):
    return asyncio.run(coro)


def test_launch_completes():
    async def go():
        manager = TaskManager()
        bt = manager.launch(_FakeAgent("ok"), object(), "do it")
        await bt.future
        return manager, bt

    manager, bt = _run(go())
    assert bt.status == "completed"
    assert bt.result == "ok"
    assert manager.get(bt.id) is bt


def test_launch_failed():
    async def go():
        manager = TaskManager()
        bt = manager.launch(_FakeAgent(fail=True), object(), "do it")
        await bt.future
        return bt

    bt = _run(go())
    assert bt.status == "failed" and "boom" in bt.error


def test_task_list_get():
    async def go():
        manager = TaskManager()
        bt = manager.launch(_FakeAgent("ok"), object(), "do it", name="worker")
        await bt.future
        return manager, bt

    manager, bt = _run(go())
    listing = TaskListTool(manager).run()
    assert bt.id in listing.content
    detail = TaskGetTool(manager).run(bt.id)
    assert "completed" in detail.content

    missing = TaskGetTool(manager).run("nope")
    assert not missing.ok


def test_task_stop():
    async def go():
        manager = TaskManager()
        bt = manager.launch(_FakeAgent(delay=5), object(), "slow")
        await asyncio.sleep(0.01)
        ok = manager.stop(bt.id)
        try:
            await bt.future
        except asyncio.CancelledError:
            pass
        return ok, bt

    ok, bt = _run(go())
    assert ok is True
    assert bt.status == "cancelled"
    assert TaskStopTool(TaskManager()).run("nope").ok is False


def test_on_done_notification():
    seen = []

    async def go():
        manager = TaskManager(on_done=seen.append)
        bt = manager.launch(_FakeAgent("ok"), object(), "do it")
        await bt.future
        return bt

    bt = _run(go())
    assert seen and seen[0] is bt
