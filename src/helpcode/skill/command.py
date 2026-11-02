"""/skills 命令与 Skill 斜杠命令的 handler 工厂。"""

from __future__ import annotations

from ..command import Command, CommandContext, CommandType
from .manager import SkillManager


def _make_skills_handler(manager: SkillManager):
    """`/skills`：列出可用 / 已激活 Skill；`reload` 触发热更新。"""

    async def handler(ctx: CommandContext) -> None:
        if ctx.args.strip() == "reload":
            warnings = manager.reload()
            msg = "已重新加载 Skill"
            if warnings:
                msg += "（" + "；".join(warnings) + "）"
            ctx.host.show(msg)
            return
        lines = [manager.render_available().strip()]
        active = manager.active_names()
        lines.append(
            "已激活：" + (", ".join(f"/{n}" for n in active) if active else "（无）")
        )
        ctx.host.show("\n".join(l for l in lines if l))

    return handler


async def _skill_command(ctx: CommandContext) -> None:
    """Skill 的斜杠短命令：交给宿主按执行模式跑（shared 送对话 / isolated 回流摘要）。"""
    summary = await ctx.host.run_skill(ctx.command.name, ctx.args)
    if summary:
        ctx.host.show(summary)


def register_skill_commands(registry, manager: SkillManager) -> None:
    """把 /skills 命令注册进命令注册中心。"""
    registry.register(
        Command(
            name="skills",
            description="列出 / 重载 Skill（reload 热更新）",
            usage="/skills [reload]",
            type=CommandType.LOCAL,
            handler=_make_skills_handler(manager),
            arg_hint="[reload]",
        )
    )


def make_skill_command(skill) -> Command:
    """为某个 Skill 生成一条斜杠短命令（/名字）。"""
    return Command(
        name=skill.name,
        description=skill.description,
        usage=f"/{skill.name} [参数]",
        type=CommandType.PROMPT,
        handler=_skill_command,
        arg_hint="<参数>",
    )
