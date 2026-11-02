"""Agent Team：Lead + 队员 + 共享任务 + 邮箱。

对外只暴露编排入口与数据结构；锁、后端、邮箱细节留在各自模块里。
"""

from .backend import (
    Backend,
    BackendError,
    BackendType,
    SpawnRequest,
    detect_backend,
    make_backend,
)
from .coordinator import COORDINATOR_TOOLS, coordinator_prompt, is_coordinator_mode
from .mailbox import MESSAGE_TYPES, Mailbox, Message
from .manager import Manager
from .model import (
    Team,
    TeamError,
    TeamHasActiveMembersError,
    TeammateInfo,
    TeamNotFoundError,
    sanitize_name,
)
from .service import TeamService
from .tasks import STATUSES, SharedTask, TaskStore

__all__ = [
    "COORDINATOR_TOOLS",
    "MESSAGE_TYPES",
    "STATUSES",
    "Backend",
    "BackendError",
    "BackendType",
    "Mailbox",
    "Manager",
    "Message",
    "SharedTask",
    "SpawnRequest",
    "TaskStore",
    "Team",
    "TeamError",
    "TeamHasActiveMembersError",
    "TeamNotFoundError",
    "TeamService",
    "TeammateInfo",
    "coordinator_prompt",
    "detect_backend",
    "is_coordinator_mode",
    "make_backend",
    "sanitize_name",
]
