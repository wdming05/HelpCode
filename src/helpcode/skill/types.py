"""Skill 的类型与异常。本模块不依赖任何 UI 库。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ExecutionMode(Enum):
    """Skill 的执行模式。

    - SHARED：共享当前对话，结果留在主历史。
    - ISOLATED：开独立对话跑完，把摘要回流主历史。
    """

    SHARED = "shared"
    ISOLATED = "isolated"


# 系统级工具名（两阶段加载的第二阶段入口）。白名单收窄时恒保留它（F11）。
# 放在 types 里避免 manager↔tool 的循环导入。
LOAD_SKILL_TOOL = "load_skill"


@dataclass(frozen=True)
class Skill:
    """一个 Skill 的完整元数据与 SOP 正文。

    ``name`` 在解析时统一小写（斜杠命令大小写不敏感，见 ch10）。
    ``body`` 是发给模型的 SOP 指令，可含 ``{{args}}`` / ``{{N}}`` 占位符。
    ``tools`` 是可见工具白名单；空元组表示不限制。
    """

    name: str
    description: str
    body: str
    tools: tuple[str, ...] = ()
    mode: ExecutionMode = ExecutionMode.SHARED
    history: int = 0
    model: str | None = None
    source: str = "builtin"  # builtin / user / project
    path: Path | None = None
    directory: Path | None = None


class SkillError(RuntimeError):
    """Skill 加载或装配阶段的错误。"""


class WhitelistError(SkillError):
    """白名单引用了不存在的工具。启动期必须终止进程。"""

    def __init__(self, skill_name: str, tool: str) -> None:
        self.skill_name = skill_name
        self.tool = tool
        super().__init__(f"Skill「{skill_name}」的白名单引用了不存在的工具：{tool}")
