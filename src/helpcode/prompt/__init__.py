"""系统提示装配：模块化 + 环境信息 + 补充消息 + banner。"""

from .environment import Environment, gather_environment
from .modules import Module, assemble_system, fixed_modules, optional_modules
from .reminder import EXECUTE_DIRECTIVE, plan_reminder, system_reminder

__all__ = [
    "CAT_BANNER",
    "EXECUTE_DIRECTIVE",
    "READY_HINT",
    "Environment",
    "Module",
    "assemble_system",
    "build_system_prompt",
    "fixed_modules",
    "gather_environment",
    "optional_modules",
    "plan_reminder",
    "render_banner",
    "system_reminder",
]


def build_system_prompt(
    instructions: str = "",
    memory: str = "",
    active_skills: str = "",
    available_skills: str = "",
) -> str:
    """装配稳定 system 文本（七个固定模块 + 四个可选槽），不含环境/时间内容。

    这些文本进的是**稳定段**：它们在会话内是快照（装配层只算一次），
    所以改笔记文件不会让下一轮的 stable 变化，缓存仍然命中（N1）。
    「已激活 Skill」由 Agent 每轮从 SkillManager 现读，激活状态变化才会变。
    """
    return assemble_system(
        fixed_modules()
        + optional_modules(instructions, memory, active_skills, available_skills)
    )


# ASCII 猫 banner
CAT_BANNER = "  /\\_/\\\n ( o.o )\n  > ^ <\n"

# 就绪提示行
READY_HINT = "就绪。输入你的问题，按 Enter 发送；/exit 退出。"


def render_banner(version: str, cwd: str) -> str:
    """拼出「猫 + 应用名与版本 + 工作目录 + 就绪提示行」。"""
    return f"{CAT_BANNER}  HelpCode v{version}\n  工作目录: {cwd}\n  {READY_HINT}\n"
