"""团队共享任务表：所有人读写同一个 ``tasks.json``。

任务 id 形如 ``task-1``；删除是软删除（状态置 ``deleted``），
这样引用它的 blocks/blocked_by 不会变成悬空 id。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .state import locked_json, read_json

STATUSES = ("pending", "in_progress", "completed", "deleted")


class TaskError(RuntimeError):
    """任务相关错误（不存在 / 状态非法）。"""


@dataclass
class SharedTask:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str = ""
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    created: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "owner": self.owner,
            "blocks": self.blocks,
            "blocked_by": self.blocked_by,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> SharedTask:
        def ids(key: str) -> list[str]:
            value = raw.get(key)
            return [str(v) for v in value] if isinstance(value, list) else []

        return cls(
            id=str(raw.get("id") or ""),
            subject=str(raw.get("subject") or ""),
            description=str(raw.get("description") or ""),
            status=str(raw.get("status") or "pending"),
            owner=str(raw.get("owner") or ""),
            blocks=ids("blocks"),
            blocked_by=ids("blocked_by"),
            created=float(raw.get("created") or 0.0),
        )

    def render(self) -> str:
        state = self.status
        owner = f" @{self.owner}" if self.owner else ""
        deps = ""
        if self.blocked_by:
            deps = f"（等待 {', '.join(self.blocked_by)}）"
        return f"{self.id} [{state}] {self.subject}{owner}{deps}"


class TaskStore:
    """团队任务表。所有改法都走 ``locked_json``，与邮箱共用一把锁实现。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict:
        data = read_json(self.path)
        if not isinstance(data.get("tasks"), list):
            data["tasks"] = []
        return data

    def create(
        self, subject: str, description: str = "", owner: str = ""
    ) -> SharedTask:
        if not subject.strip():
            raise TaskError("任务标题不能为空")
        with locked_json(self.path) as data:
            tasks = data.get("tasks")
            tasks = tasks if isinstance(tasks, list) else []
            next_no = int(data.get("next") or 1)
            task = SharedTask(
                id=f"task-{next_no}",
                subject=subject.strip(),
                description=description,
                owner=owner,
                created=time.time(),
            )
            tasks.append(task.to_dict())
            data["tasks"] = tasks
            data["next"] = next_no + 1
        return task

    def get(self, task_id: str) -> SharedTask | None:
        for item in self._load()["tasks"]:
            if isinstance(item, dict) and item.get("id") == task_id:
                return SharedTask.from_dict(item)
        return None

    def list(self, include_deleted: bool = False) -> list[SharedTask]:
        out = []
        for item in self._load()["tasks"]:
            if not isinstance(item, dict):
                continue
            task = SharedTask.from_dict(item)
            if task.status == "deleted" and not include_deleted:
                continue
            out.append(task)
        return sorted(out, key=lambda t: t.created)

    def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        owner: str | None = None,
        subject: str | None = None,
        description: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
    ) -> SharedTask:
        if status is not None and status not in STATUSES:
            raise TaskError(f"非法状态：{status}（可用：{', '.join(STATUSES)}）")
        with locked_json(self.path) as data:
            tasks = data.get("tasks")
            tasks = tasks if isinstance(tasks, list) else []
            for item in tasks:
                if not isinstance(item, dict) or item.get("id") != task_id:
                    continue
                if status is not None:
                    item["status"] = status
                if owner is not None:
                    item["owner"] = owner
                if subject is not None:
                    item["subject"] = subject
                if description is not None:
                    item["description"] = description
                if add_blocks:
                    item["blocks"] = _merge(item.get("blocks"), add_blocks)
                if add_blocked_by:
                    item["blocked_by"] = _merge(item.get("blocked_by"), add_blocked_by)
                data["tasks"] = tasks
                return SharedTask.from_dict(item)
        raise TaskError(f"任务不存在：{task_id}")


def _merge(existing, extra) -> list[str]:
    out = [str(v) for v in existing] if isinstance(existing, list) else []
    for value in extra:
        if value and value not in out:
            out.append(str(value))
    return out
