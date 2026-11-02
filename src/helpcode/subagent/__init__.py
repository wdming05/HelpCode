"""SubAgent 机制：角色目录、子 Agent 运行、后台任务与四个工具。"""

from .catalog import Catalog, Role, load_catalog, parse_role
from .runner import ParentContext, SubAgentRunner, mode_for
from .task import BackgroundTask, TaskManager
from .tools import (
    AgentTool,
    TaskGetTool,
    TaskListTool,
    TaskStopTool,
    filter_registry,
)

__all__ = [
    "AgentTool",
    "BackgroundTask",
    "Catalog",
    "ParentContext",
    "Role",
    "SubAgentRunner",
    "TaskGetTool",
    "TaskListTool",
    "TaskManager",
    "TaskStopTool",
    "filter_registry",
    "load_catalog",
    "mode_for",
    "parse_role",
]
