"""Team / TeammateInfo / 名字规范化。

团队配置是**跨进程共享**的（窗格后端的队员会读它判断自己属于谁），
所以每次成员变更都要立刻落盘，不能攒到退出时一起写。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .state import read_json, write_json

# 允许出现在目录名里的字符：字母数字（含中文）、点、下划线、连字符。
# 空格与路径分隔符一律替换——团队名会变成路径，混进分隔符就是路径穿越。
_ALLOWED = re.compile(r"[^\w.-]", re.UNICODE)
_DASHES = re.compile(r"-{2,}")
# 这两个是路径语义里的特殊段，绝不能当目录名
_RESERVED = {".", ".."}


class TeamError(RuntimeError):
    """团队相关错误的基类。"""


class TeamNotFoundError(TeamError):
    """团队不存在。"""


class TeamHasActiveMembersError(TeamError):
    """还有成员没空闲，不能删团队。"""


def sanitize_name(name: str) -> str:
    """把任意团队名变成安全目录名；无法得到非空结果时抛错。"""
    text = _ALLOWED.sub("-", (name or "").strip())
    text = _DASHES.sub("-", text).strip("-")
    if not text or text in _RESERVED:
        raise TeamError(f"团队名非法（清洗后为空或保留名）：{name!r}")
    return text


@dataclass(frozen=True)
class TeammateInfo:
    """花名册里的一行。"""

    name: str
    agent_id: str
    agent_type: str = ""
    model: str = ""
    worktree_path: str = ""
    branch: str = ""
    backend_type: str = "in-process"
    pane_id: str = ""
    # None / True 都算活跃；只有 False 才是「干完停下了」
    is_active: bool | None = None
    plan_mode_required: bool = False
    session_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "model": self.model,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "backend_type": self.backend_type,
            "pane_id": self.pane_id,
            "is_active": self.is_active,
            "plan_mode_required": self.plan_mode_required,
            "session_dir": self.session_dir,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> TeammateInfo:
        def text(key: str) -> str:
            value = raw.get(key, "")
            return str(value) if isinstance(value, (str, int)) and value != "" else ""

        active = raw.get("is_active", None)
        return cls(
            name=text("name"),
            agent_id=text("agent_id"),
            agent_type=text("agent_type"),
            model=text("model"),
            worktree_path=text("worktree_path"),
            branch=text("branch"),
            backend_type=text("backend_type") or "in-process",
            pane_id=text("pane_id"),
            is_active=active if isinstance(active, bool) else None,
            plan_mode_required=bool(raw.get("plan_mode_required", False)),
            session_dir=text("session_dir"),
        )


@dataclass
class Team:
    """一个长期存在的小组。"""

    name: str
    sanitized_name: str
    description: str
    lead_agent_id: str
    config_dir: Path
    backend: str = "in-process"
    created_at: float = field(default_factory=time.time)
    members: list[TeammateInfo] = field(default_factory=list)

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def mailbox_root(self) -> Path:
        return self.config_dir / "mailboxes"

    @property
    def tasks_path(self) -> Path:
        return self.config_dir / "tasks.json"

    def member(self, name: str) -> TeammateInfo | None:
        return next((m for m in self.members if m.name == name), None)

    # ------------------------------------------------------------ 变更

    def add_member(self, info: TeammateInfo) -> None:
        if self.member(info.name) is not None:
            raise TeamError(f"成员已存在：{info.name}")
        self.members.append(info)
        self.save()

    def set_member_active(self, name: str, active: bool) -> None:
        """更新活跃位。只替换这一条，保留其它字段（frozen 所以整条重建）。"""
        for index, info in enumerate(self.members):
            if info.name != name:
                continue
            data = info.to_dict()
            data["is_active"] = active
            self.members[index] = TeammateInfo.from_dict(data)
            self.save()
            return
        raise TeamNotFoundError(f"成员不存在：{name}")

    def remove_member(self, name: str) -> None:
        before = len(self.members)
        self.members = [m for m in self.members if m.name != name]
        if len(self.members) == before:
            raise TeamNotFoundError(f"成员不存在：{name}")
        self.save()

    # ------------------------------------------------------------ 落盘

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "sanitized_name": self.sanitized_name,
            "description": self.description,
            "lead_agent_id": self.lead_agent_id,
            "backend": self.backend,
            "created_at": self.created_at,
            "members": [m.to_dict() for m in self.members],
        }

    def save(self) -> None:
        write_json(self.config_path, self.to_dict())

    @classmethod
    def load(cls, config_path: Path) -> Team:
        """从 ``config.json`` 还原；缺字段一律给默认值，坏文件抛错由调用方跳过。"""
        raw = read_json(config_path)
        if not raw:
            raise TeamError(f"团队配置为空或损坏：{config_path}")
        sanitized = str(raw.get("sanitized_name") or "") or sanitize_name(
            str(raw.get("name") or "")
        )
        members_raw = raw.get("members")
        members = [
            TeammateInfo.from_dict(item)
            for item in (members_raw if isinstance(members_raw, list) else [])
            if isinstance(item, dict) and item.get("name")
        ]
        return cls(
            name=str(raw.get("name") or sanitized),
            sanitized_name=sanitized,
            description=str(raw.get("description") or ""),
            lead_agent_id=str(raw.get("lead_agent_id") or "lead"),
            config_dir=Path(config_path).parent,
            backend=str(raw.get("backend") or "in-process"),
            created_at=float(raw.get("created_at") or time.time()),
            members=members,
        )
