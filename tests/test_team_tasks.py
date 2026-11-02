"""共享任务：增删查改、依赖、软删除、非法状态。"""

import pytest

from helpcode.team.tasks import TaskError, TaskStore


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "tasks.json")


def test_create_get_list(store):
    t1 = store.create("拆模块", "先拆配置", owner="alice")
    t2 = store.create("写测试")
    assert t1.id == "task-1" and t2.id == "task-2"
    assert store.get("task-1").owner == "alice"
    assert [t.id for t in store.list()] == ["task-1", "task-2"]
    assert store.get("task-9") is None


def test_create_requires_subject(store):
    with pytest.raises(TaskError):
        store.create("   ")


def test_update_status(store):
    task = store.create("跑测试")
    updated = store.update(task.id, status="in_progress", owner="bob")
    assert updated.status == "in_progress"
    assert updated.owner == "bob"
    assert store.get(task.id).status == "in_progress"


def test_dependencies(store):
    task = store.create("合并分支")
    store.update(task.id, add_blocked_by=["task-9"])
    store.update(task.id, add_blocked_by=["task-9", "task-10"])
    store.update(task.id, add_blocks=["task-11"])
    got = store.get(task.id)
    assert got.blocked_by == ["task-9", "task-10"]  # 去重
    assert got.blocks == ["task-11"]


def test_soft_delete(store):
    task = store.create("待删")
    store.update(task.id, status="deleted")
    assert store.list() == []
    assert [t.id for t in store.list(include_deleted=True)] == [task.id]


def test_bad_status_rejected(store):
    task = store.create("x")
    with pytest.raises(TaskError):
        store.update(task.id, status="done")


def test_update_missing_task(store):
    with pytest.raises(TaskError):
        store.update("task-404", status="completed")


def test_render_shows_deps(store):
    task = store.create("等依赖")
    store.update(task.id, add_blocked_by=["task-0"])
    assert "task-0" in store.get(task.id).render()
