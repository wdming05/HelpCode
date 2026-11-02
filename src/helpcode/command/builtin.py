"""十个内置命令 + 隐藏的 exit，以及默认注册表工厂。"""

from __future__ import annotations

from ..permission import Mode
from ..prompt import EXECUTE_DIRECTIVE
from .registry import CommandRegistry
from .types import Command, CommandContext, CommandType

# /permission 额外接受的写法（str(mode) 覆盖不了带下划线的形式）
_MODE_EXTRA: dict[str, Mode] = {
    "accept_edits": Mode.ACCEPT_EDITS,
    "bypass_permissions": Mode.BYPASS,
}


def _parse_mode(s: str) -> tuple[Mode, bool]:
    key = s.strip().lower()
    if key in _MODE_EXTRA:
        return _MODE_EXTRA[key], True
    for m in Mode:
        if str(m).lower() == key:
            return m, True
    return Mode.DEFAULT, False


def _line(cmd: Command) -> str:
    name = cmd.name
    if cmd.aliases:
        name += " [" + " ".join(cmd.aliases) + "]"
    return f"/{name:<26} {cmd.description}"


async def _cmd_help(ctx: CommandContext) -> None:
    arg = ctx.args.strip()
    if arg:
        cmd = ctx.registry.resolve(arg.lstrip("/"))
        if cmd is None:
            ctx.host.show(f"未找到命令 /{arg}，输入 /help 查看全部命令")
            return
        alias = f"（别名：{', '.join(cmd.aliases)}）" if cmd.aliases else ""
        ctx.host.show(
            f"/{cmd.name} {alias}\n  描述：{cmd.description}\n  用法：{cmd.usage}"
        )
        return
    lines = [_line(c) for c in ctx.registry.visible()]
    lines.append("输入 /help <命令> 查看详情")
    ctx.host.show("\n".join(lines))


async def _cmd_status(ctx: CommandContext) -> None:
    snap = ctx.host.snapshot()
    rows = [
        f"模型：{snap.model or '（未选择）'}",
        f"会话：{snap.session_id or '（无）'}",
        f"消息：{snap.message_count} 条",
        f"上下文：{snap.context_used} / {snap.context_window} token",
        f"模式：[{snap.mode.name}]",
    ]
    if snap.usage is not None:
        rows.append(
            f"用量：in {snap.usage.input_tokens} / out {snap.usage.output_tokens}"
            f" / 缓存命中 {snap.usage.cache_read}"
        )
    else:
        rows.append("用量：本轮尚未请求")
    ctx.host.show("\n".join(rows))


async def _cmd_memory(ctx: CommandContext) -> None:
    m = ctx.host.snapshot().memory
    lines = ["指令文件（三层）："]
    for label, hit in m.layers:
        lines.append(f"  {'✓' if hit else '—'} {label}")
    lines.append("记忆索引：")
    for label, n in m.index_lines:
        lines.append(f"  {label}：{n} 行")
    lines.append(f"笔记文件：{m.note_count} 个")
    if m.warnings:
        lines.append("告警：")
        lines += [f"  - {w}" for w in m.warnings]
    ctx.host.show("\n".join(lines))


async def _cmd_plan(ctx: CommandContext) -> None:
    ctx.host.set_mode(Mode.PLAN)
    ctx.host.refresh_status()
    ctx.host.show("已进入计划模式（仅只读工具，可 /do 退回执行）")


async def _cmd_do(ctx: CommandContext) -> None:
    ctx.host.set_mode(Mode.DEFAULT)
    ctx.host.refresh_status()
    await ctx.host.send_prompt(EXECUTE_DIRECTIVE)


async def _cmd_permission(ctx: CommandContext) -> None:
    arg = ctx.args.strip()
    if not arg:
        cur = ctx.host.get_mode()
        ctx.host.show(
            f"当前模式：{cur.name}\n可选：default / acceptEdits / plan / bypass"
        )
        return
    mode, ok = _parse_mode(arg)
    if not ok:
        ctx.host.show_error(
            f"未知模式：{arg}（可选 default / acceptEdits / plan / bypass），模式未改变"
        )
        return
    ctx.host.set_mode(mode)
    ctx.host.refresh_status()
    ctx.host.show(f"已切换到 {mode.name}")


async def _cmd_compact(ctx: CommandContext) -> None:
    notice = await ctx.host.compact()
    ctx.host.show(notice or "无需压缩")


async def _cmd_clear(ctx: CommandContext) -> None:
    sid = await ctx.host.start_new_session()
    if sid is None:
        ctx.host.show_error("无法创建新会话，历史未清空")
        return
    ctx.host.clear_screen()
    ctx.host.show(f"已开始新会话 {sid}")


async def _cmd_session(ctx: CommandContext) -> None:
    sessions = ctx.host.snapshot().sessions
    if not sessions:
        ctx.host.show("还没有历史会话")
        return
    lines = ["最近会话："]
    for m in sessions[:5]:
        lines.append(f"  {m.session_id}  {m.title}  ·  {m.messages} 条  ·  {m.model}")
    ctx.host.show("\n".join(lines))
    ctx.host.open_session_picker()


async def _cmd_review(ctx: CommandContext) -> None:
    scope = ctx.args.strip() or "当前未提交的改动"
    prompt = (
        f"请审查「{scope}」：\n"
        "1. 先定位到具体文件与行号，说明问题在哪；\n"
        "2. 按「正确性 / 可维护性 / 安全风险」三档给结论；\n"
        "3. 只指出真实问题，不要泛泛而谈。"
    )
    await ctx.host.send_prompt(prompt)


async def _cmd_exit(ctx: CommandContext) -> None:
    await ctx.host.quit()


_WORKTREE_USAGE = """用法：
  /worktree create <名字>               创建并进入（之后的文件操作都在里面）
  /worktree enter <名字>                进入已有的 Worktree（不创建）
  /worktree list                        列出活跃 Worktree
  /worktree exit [--remove]             退出当前 Worktree（--remove 顺带删除）
  /worktree remove <名字> [--discard]   删除指定 Worktree（--discard 丢弃改动）
名字只允许字母、数字、. _ -，可用 / 分层（如 feat/login）。"""


async def _cmd_worktree(ctx: CommandContext) -> None:
    """ch14：Worktree 管理。参数解析留在命令层，具体动作交给宿主。"""
    raw = ctx.args.strip()
    if not raw:
        ctx.host.show(_WORKTREE_USAGE)
        return
    sub, _, rest = raw.partition(" ")
    text = await ctx.host.worktree(sub.lower(), rest.strip())
    ctx.host.show(text)


_TEAM_USAGE = """用法：
  /team create <名字> [描述]   创建团队并把当前 Agent 设为 Lead
  /team list                   列出团队（后端、成员数、活跃数）
  /team info <名字>            成员明细（角色、状态、分支、工作目录）
  /team kill <成员>            终止某个成员
  /team delete <名字> [--force] 删除团队（有活跃成员需 --force）"""


async def _cmd_team(ctx: CommandContext) -> None:
    """ch15：Team 管理。解析与展示留在命令层，动作交给宿主。"""
    raw = ctx.args.strip()
    if not raw:
        ctx.host.show(_TEAM_USAGE)
        return
    sub, _, rest = raw.partition(" ")
    text = await ctx.host.team(sub.lower(), rest.strip())
    ctx.host.show(text)


BUILTINS: list[Command] = [
    Command(
        name="help",
        description="列出全部命令 / 查看某个命令的用法",
        usage="/help [命令]",
        type=CommandType.LOCAL,
        handler=_cmd_help,
        aliases=("?",),
        arg_hint="<命令>",
    ),
    Command(
        name="compact",
        description="立即压缩当前历史",
        usage="/compact",
        type=CommandType.LOCAL,
        handler=_cmd_compact,
    ),
    Command(
        name="clear",
        description="开一个新会话（旧会话保留）",
        usage="/clear",
        type=CommandType.UI_STATE,
        handler=_cmd_clear,
    ),
    Command(
        name="plan",
        description="进入计划模式（仅只读工具）",
        usage="/plan",
        type=CommandType.UI_STATE,
        handler=_cmd_plan,
    ),
    Command(
        name="do",
        description="退回执行模式，并按计划开始执行",
        usage="/do",
        type=CommandType.PROMPT,
        handler=_cmd_do,
    ),
    Command(
        name="session",
        description="列出历史会话并进入选择界面",
        usage="/session",
        type=CommandType.UI_STATE,
        handler=_cmd_session,
        aliases=("resume", "sessions"),
    ),
    Command(
        name="memory",
        description="查看指令文件与记忆索引概况",
        usage="/memory",
        type=CommandType.LOCAL,
        handler=_cmd_memory,
    ),
    Command(
        name="permission",
        description="查看或切换权限模式",
        usage="/permission [模式]",
        type=CommandType.UI_STATE,
        handler=_cmd_permission,
        aliases=("perm",),
        arg_hint="<模式>",
    ),
    Command(
        name="status",
        description="查看模型、会话、上下文与用量",
        usage="/status",
        type=CommandType.LOCAL,
        handler=_cmd_status,
    ),
    Command(
        name="worktree",
        description="管理隔离工作目录（Git Worktree）",
        usage="/worktree <create|enter|list|exit|remove> [参数]",
        type=CommandType.UI_STATE,
        handler=_cmd_worktree,
        arg_hint="<子命令>",
    ),
    Command(
        name="team",
        description="管理协作团队（Lead 与队员）",
        usage="/team <create|list|info|kill|delete> [参数]",
        type=CommandType.UI_STATE,
        handler=_cmd_team,
        arg_hint="<子命令>",
    ),
    Command(
        name="review",
        description="审查指定范围（默认当前改动）",
        usage="/review [范围]",
        type=CommandType.PROMPT,
        handler=_cmd_review,
        arg_hint="<范围>",
    ),
    Command(
        name="exit",
        description="退出程序",
        usage="/exit",
        type=CommandType.UI_STATE,
        handler=_cmd_exit,
        aliases=("quit", "q"),
        hidden=True,
    ),
]


def build_default_registry() -> CommandRegistry:
    """构造默认注册表；任何别名冲突会在这里直接抛出。"""
    registry = CommandRegistry()
    for cmd in BUILTINS:
        registry.register(cmd)
    return registry
