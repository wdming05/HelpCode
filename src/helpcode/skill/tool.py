"""load_skill 工具：两阶段加载的第二阶段。系统级、只读。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..tool import ToolResult
from .types import LOAD_SKILL_TOOL

if TYPE_CHECKING:
    from .manager import SkillManager


class SkillTool:
    """按需加载一个 Skill 的完整指令。

    ``on_activate`` 回调在成功激活后被调用，用于让上层（app）注册斜杠短命令。
    """

    name = LOAD_SKILL_TOOL
    read_only = True  # 只读：PLAN 模式也可用，且不触发权限审批
    description = "按需加载一个 Skill 的完整指令（两阶段加载的第二阶段）"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "要加载的 Skill 名字"},
            "args": {
                "type": "string",
                "description": "传给 SOP 的参数，替换正文里的 {{args}}/{{N}} 占位符",
            },
        },
        "required": ["name"],
    }

    def __init__(self, manager: SkillManager, on_activate=None) -> None:
        self._manager = manager
        self._on_activate = on_activate

    def run(self, name: str, args: str = "") -> ToolResult:
        skill = self._manager.get(name)
        if skill is None:
            return ToolResult(
                ok=False,
                content=(
                    f"未找到 Skill：{name}。"
                    "输入 /skills 查看可用 Skill，或用 load_skill 传一个已知名字。"
                ),
                is_error=True,
            )
        self._manager.activate(skill.name)
        if self._on_activate is not None:
            self._on_activate(skill)
        return ToolResult(ok=True, content=self._manager.load_text(skill, args or ""))
