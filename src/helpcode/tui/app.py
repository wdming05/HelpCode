"""TUI App：HelpCodeApp、状态机与布局。"""

import asyncio
import os
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from .. import __version__
from ..agent import Agent, PermissionRequest
from ..command import (
    CommandContext,
    DuplicateCommandError,
    ParsedCommand,
    build_default_registry,
    parse,
)
from ..config import DEFAULT_WINDOW, ProviderConfig
from ..context import ContextManager
from ..conversation import Conversation
from ..hook.events import HookEvent
from ..llm import Provider, new_provider
from ..memory import notes
from ..memory.session import tool_results_dir
from ..permission import Mode, Outcome, friendly_name, new_engine
from ..prompt import render_banner
from ..skill import SkillTool, make_skill_command, substitute
from ..skill.types import ExecutionMode
from ..subagent import (
    AgentTool,
    ParentContext,
    SubAgentRunner,
    TaskGetTool,
    TaskListTool,
    TaskManager,
    TaskStopTool,
)
from ..team import COORDINATOR_TOOLS, coordinator_prompt
from ..team.service import TeamService
from ..team.tools import TeamCreateTool
from ..tool import new_default_registry
from ..tracing import Tracer
from ..worktree import ExitAction, WorktreeHasChangesError
from . import SessionState
from .host import TextualHost
from .resume import ResumeMixin
from .select import SelectMixin
from .stream import StreamMixin
from .view import (
    render_cleanup,
    render_notice,
    render_permission_request,
    render_repaired,
    render_restored,
    render_statusbar,
    user_block,
)


class InputArea(TextArea):
    """输入框：Enter 提交，Alt+Enter 换行。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "❯"
        self.border_subtitle = "Send a message..."

    async def _on_key(self, event) -> None:
        """拦截 Enter（提交）、Tab（补全）与 Alt+Enter（换行）。"""
        key = event.key
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.action_submit_input()
            return
        if key == "tab":
            app = self.app
            if isinstance(app, HelpCodeApp):
                event.stop()
                event.prevent_default()
                app.handle_tab()
                return
        if key == "alt+enter":
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self._replace_via_keyboard("\n", start, end)
            return
        await super()._on_key(event)

    def action_submit_input(self) -> None:
        """Enter 提交：把文本交给 App 处理。"""
        app = self.app
        if isinstance(app, HelpCodeApp):
            asyncio.create_task(app.submit(self.text))


# 子 Agent / 队员的审批最多等主对话空闲这么久；等不到按拒绝处理，绝不无限挂住
_APPROVAL_WAIT_SECONDS = 120.0


class HelpCodeApp(StreamMixin, SelectMixin, ResumeMixin, App):
    """HelpCode 终端界面。"""

    CSS = """
    #log {
        height: 1fr;
        border: none;
    }
    #streaming {
        height: auto;
        min-height: 0;
        padding: 0 1;
    }
    #input {
        height: auto;
        border: solid $primary;
    }
    #statusbar {
        height: 1;
        padding: 0 1;
    }
    #provider_list {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "退出"),
        ("escape", "cancel", "取消"),
    ]

    def __init__(
        self,
        providers: list[ProviderConfig],
        mcp_tools: list | None = None,
        *,
        instructions: str = "",
        memory: str = "",
        conv: Conversation | None = None,
        writer=None,
        restore=None,
        cleanup=None,
        sessions: list | None = None,
        session_id: str | None = None,
        commands=None,
        instruction_set=None,
        index_bundle=None,
        skill_manager=None,
        hooks=None,
        catalog=None,
        worktrees=None,
        teams=None,
        coordinator: bool = False,
    ) -> None:
        super().__init__()
        self.providers = providers
        self.mcp_tools = mcp_tools or []
        self.state = SessionState.SELECTING
        self.provider: Provider | None = None
        self.provider_cfg: ProviderConfig | None = None
        self.agent: Agent | None = None
        self.engine = None
        # 恢复时传入已有的 Conversation；新建时自己造一个
        self.conv = conv if conv is not None else Conversation()
        self.writer = writer
        self.session_id = session_id
        self._restore = restore
        self._cleanup = cleanup
        self.sessions = sessions or []
        self._resume_items: list = []
        # 快照：整个会话不变，改笔记文件不会让 stable 变形（N1）
        self._instructions = instructions
        self._memory = memory
        # 命令系统：注册表 + /memory 需要的原始加载结果
        self.commands = commands
        self._instruction_set = instruction_set
        self._index_bundle = index_bundle
        # Skill 系统：管理器（激活集合 / 渲染 / 白名单）
        self.skill_manager = skill_manager
        # Hook 系统：生命周期引擎（Session 级与 UserPromptSubmit 由本类触发）
        self.hooks = hooks
        # ch13：角色目录 + 后台任务 + 子 Agent 运行器（_setup_agent 里装配）
        self.catalog = catalog
        self.task_manager: TaskManager | None = None
        self.subagent_runner: SubAgentRunner | None = None
        # 后台任务完成通知，下一次发送前拼进用户消息
        self._pending_notices: list[str] = []
        # ch14：Worktree 管理器（非 git 仓库时为 None）+ 当前主 Agent 的工作目录
        self.worktrees = worktrees
        self.worktree_cwd: str | None = None
        # ch15：团队管理器（cli 装配）+ 团队服务（provider 选定后才能建，见 _setup_agent）
        self.team_manager = teams
        self.team_service = None
        self.coordinator = coordinator
        self.last_usage = None  # 最近一次请求的 token 用量（/status 用）
        self.cancel_event = asyncio.Event()
        self.mode = Mode.DEFAULT
        self.cur_reply = ""
        self._cur_iteration = 0
        self.turn_start = 0.0
        self._stream_task = None
        self._timer = None
        self._pending_request = None
        self._approve_cursor = 0
        # 子 Agent 审批串行锁：同一时刻只弹一个（主对话可能自己也在弹）
        self._approval_lock = asyncio.Lock()
        # 恢复的会话若已贴窗口，压缩在后台跑；第一轮请求前必须等它收尾（AC16）
        self._restore_compress_task = None

    def compose(self) -> ComposeResult:
        yield RichLog(id="log", wrap=True, markup=False)
        yield Static("", id="streaming")
        yield InputArea(id="input")
        yield Static("", id="statusbar")
        if len(self.providers) > 1:
            yield OptionList(
                *[Option(f"{p.name} ({p.model})") for p in self.providers],
                id="provider_list",
            )
        # 选会话用的两个控件：默认隐藏，/resume 时显示
        yield Input(id="resume_filter", placeholder="输入关键字过滤会话…")
        yield OptionList(id="resume_list")
        # Tab 补全用的候选菜单：默认隐藏
        yield OptionList(id="command_list")

    def on_mount(self) -> None:
        """启动：写 banner 与恢复/清理提示；单 provider 直进 IDLE，多 provider 进 SELECTING。"""
        log = self.query_one("#log", RichLog)
        log.write(render_banner(__version__, os.getcwd()))

        # ch09：三个可选提示行，都是「有内容才写」
        if self._restore is not None:
            log.write(
                render_restored(self.session_id or "-", len(self._restore.messages))
            )
            repaired = render_repaired(self._restore.summary_line())
            if repaired.plain:
                log.write(repaired)
        if self._cleanup is not None:
            cleanup_line = render_cleanup(self._cleanup.banner_line())
            if cleanup_line.plain:
                log.write(cleanup_line)

        # 选会话组件默认隐藏
        self.query_one("#resume_filter", Input).display = False
        self.query_one("#resume_list", OptionList).display = False
        self.query_one("#command_list", OptionList).display = False

        if len(self.providers) == 1:
            self.provider_cfg = self.providers[0]
            self.provider = new_provider(self.providers[0])
            self._setup_agent()
            self.state = SessionState.IDLE
            self._update_statusbar()
        else:
            self.query_one("#statusbar", Static).update(
                "请选择一个 provider（↑↓ 移动，Enter 选定）"
            )
            self._enter_selecting()

        # ch12：会话开始（首条 user 消息之前）。on_mount 是同步的，异步触发。
        if self.hooks is not None:
            asyncio.create_task(
                self._fire_hook(
                    HookEvent.SESSION_START,
                    {"session_id": self.session_id or "", "cwd": os.getcwd()},
                )
            )

    def _update_statusbar(self) -> None:
        """更新底部状态栏：左权限模式名、右模型名。"""
        if self.provider is None:
            return
        self.query_one("#statusbar", Static).update(
            render_statusbar(self.mode, self.provider.model)
        )

    def _base_registry(self):
        """基础工具池：内置 6 个 + MCP + load_skill + 任务工具（不含 Agent 工具）。"""
        registry = new_default_registry()
        # OS 沙箱状态挂在权限引擎上，bash 需要同一份引用才能被 /sandbox 切换。
        # 首次装配时 engine 可能还没建（_base_registry 会被更早的路径调到），
        # 所以用 getattr 而不是直接访问。
        engine = getattr(self, "engine", None)
        if engine is not None:
            registry.attach_sandbox(engine.sandbox)
        for tool in self.mcp_tools:
            registry.register(tool)
        if self.skill_manager is not None:
            registry.register(
                SkillTool(self.skill_manager, self._register_skill_command)
            )
        if self.task_manager is not None:
            registry.register(TaskListTool(self.task_manager))
            registry.register(TaskGetTool(self.task_manager))
            registry.register(TaskStopTool(self.task_manager))
        return registry

    def _build_registry(self):
        """主 Agent 的工具池 = 基础工具池 + Agent 工具 + 团队工具。

        `TeamCreate` 恒在（第一次建队要有入口）；协作工具跟着「有没有活跃团队」
        出现——没团队时露出任务表只会让模型乱用（F22）。
        """
        registry = self._base_registry()
        if self.subagent_runner is not None:
            registry.register(AgentTool(self.subagent_runner, self.team_service))
        if self.team_service is not None:
            # TeamCreate 恒在：它既是第一次建队的入口，也允许 Lead 再开一个团队
            registry.register(TeamCreateTool(self.team_service))
            if self.team_service.active_team is not None:
                # ch13 的 TaskList/TaskGet 与团队版同名：有团队时以团队任务表为准
                names = [
                    n for n in registry.names() if n not in ("TaskList", "TaskGet")
                ]
                registry = registry.subset(names)
                for tool in self.team_service.lead_tools():
                    registry.register(tool)
        return registry

    def _refresh_tools(self) -> None:
        """团队建/删之后重建工具列表，让协作工具立刻出现或消失。"""
        if self.agent is not None:
            self.agent.registry = self._build_registry()

    def _context_window(self) -> int:
        """窗口值取自配置对象而非 provider：provider 只该管「怎么发请求」。"""
        cfg = self.provider_cfg
        if cfg is not None and cfg.context_window:
            return cfg.context_window
        return DEFAULT_WINDOW[cfg.protocol if cfg else "openai"]

    def _build_context(self) -> ContextManager:
        return ContextManager(
            self.provider,
            Path(os.getcwd()),
            self._context_window(),
            spill_subdir=tool_results_dir(Path(os.getcwd()), self.session_id),
        )

    def _build_agent(self, on_turn_end, context: ContextManager | None = None):
        """构造一个 Agent；不共享 ContextManager（独立对话要有独立锚点）。"""
        extra = {}
        if self.coordinator:
            # ch15：协调者模式——只留调度与读类工具，并追加四阶段指引
            extra["allowed_tools"] = set(COORDINATOR_TOOLS)
            extra["system_suffix"] = coordinator_prompt()
        tracer = (
            Tracer(Path(os.getcwd()), self.session_id) if self.session_id else None
        )
        return Agent(
            self.provider,
            self._build_registry(),
            __version__,
            self.engine,
            context if context is not None else self._build_context(),
            system_extra=(self._instructions, self._memory),
            on_turn_end=on_turn_end,
            skills=self.skill_manager,
            hooks=self.hooks,
            # ch14：主 Agent 的工作目录可能是某个 Worktree（/worktree create 之后）
            cwd=self.worktree_cwd or os.getcwd(),
            tracer=tracer,
            **extra,
        )

    async def _fire_hook(self, event: HookEvent, payload: dict):
        """触发 Hook；没装配引擎时返回 None。"""
        if self.hooks is None:
            return None
        return await self.hooks.dispatch(event, payload)

    # ------------------------------------------------------------ Worktree（ch14）

    async def run_worktree(self, sub: str, args: str) -> str:
        """`/worktree` 的实现：只返回文本，渲染交给命令层。"""
        if self.worktrees is None:
            return "Worktree 未启用（当前目录不是 git 仓库根目录）"
        if sub == "create":
            return await self._worktree_create(args)
        if sub == "enter":
            return await self._worktree_enter(args)
        if sub == "list":
            return self._worktree_list()
        if sub == "exit":
            return await self._worktree_exit(remove="--remove" in args.split())
        if sub == "remove":
            return await self._worktree_remove(args)
        return f"未知子命令：{sub}（可用：create / enter / list / exit / remove）"

    async def _worktree_create(self, slug: str) -> str:
        name = slug.strip()
        if not name:
            return "用法：/worktree create <名字>"
        # 已存在时不要只丢一句「创建失败」——用户真正想做的是「进去」
        if self.worktrees.get(name) is not None:
            return (
                f"Worktree 已存在：{name}\n"
                f"· 进入它：/worktree enter {name}\n"
                f"· 删掉重建：/worktree remove {name} --discard"
            )
        try:
            # git 子进程是阻塞的：丢线程，别让界面卡住
            wt = await asyncio.to_thread(self.worktrees.create, name, manual=True)
            session = await asyncio.to_thread(self.worktrees.enter, wt.name)
        except Exception as e:
            return f"创建失败：{e}"
        self._enter_worktree(str(wt.path))
        return (
            f"已创建并进入 Worktree：{wt.path}\n"
            f"分支：{wt.branch}\n"
            f"会话：{session.session_id[:8]}（之后的文件操作都在这个目录里）"
        )

    async def _worktree_enter(self, slug: str) -> str:
        """进入一个**已存在**的 Worktree（不创建）。"""
        name = slug.strip()
        if not name:
            return "用法：/worktree enter <名字>"
        wt = self.worktrees.get(name)
        if wt is None:
            return f"没有这个 Worktree：{name}（用 /worktree list 看已有哪些）"
        try:
            session = await asyncio.to_thread(self.worktrees.enter, name)
        except Exception as e:
            return f"进入失败：{e}"
        self._enter_worktree(str(wt.path))
        return (
            f"已进入 Worktree：{wt.path}\n"
            f"分支：{wt.branch}\n"
            f"会话：{session.session_id[:8]}（之后的文件操作都在这个目录里）"
        )

    def _worktree_list(self) -> str:
        items = self.worktrees.list()
        if not items:
            return "（无 Worktree）"
        current = self.worktrees.current_session
        lines: list[str] = []
        for wt in items:
            mark = " *当前" if current and current.worktree_name == wt.name else ""
            kind = "手动" if wt.manual else "临时"
            lines.append(f"- {wt.name}  [{kind}]{mark}\n  {wt.branch}\n  {wt.path}")
        if current is None:
            lines.append("（当前不在任何 Worktree；用 /worktree enter <名字> 进入）")
        return "\n".join(lines)

    async def _worktree_exit(self, *, remove: bool) -> str:
        session = self.worktrees.current_session
        if session is None:
            return "当前不在任何 Worktree 会话中"
        action = ExitAction.REMOVE if remove else ExitAction.KEEP
        try:
            report = await asyncio.to_thread(
                self.worktrees.exit, session.worktree_name, action
            )
        except WorktreeHasChangesError:
            return (
                "退出失败：Worktree 有未提交修改或新增 commit。\n"
                f"确认丢弃请用 /worktree remove {session.worktree_name} --discard"
            )
        except Exception as e:
            return f"退出失败：{e}"
        self._leave_worktree()
        if report.removed:
            return f"已退出并删除 Worktree：{report.path}"
        return f"已退出 Worktree：{report.path}（目录保留）"

    async def _worktree_remove(self, args: str) -> str:
        parts = args.split()
        name = next((p for p in parts if not p.startswith("--")), "")
        if not name:
            return "用法：/worktree remove <名字> [--discard]"
        discard = "--discard" in parts
        try:
            report = await asyncio.to_thread(
                self.worktrees.remove, name, discard_changes=discard
            )
        except WorktreeHasChangesError:
            return "删除失败：有未提交修改或新增 commit。确认丢弃请加 --discard"
        except Exception as e:
            return f"删除失败：{e}"
        if self.worktree_cwd == report.path:
            self._leave_worktree()
        return f"已删除 Worktree：{report.path}（分支 {report.branch}）"

    def _enter_worktree(self, path: str) -> None:
        """把主 Agent 的工作目录切到 Worktree（不 chdir）。"""
        self.worktree_cwd = path
        if self.agent is not None:
            self.agent.cwd = path
        # 模型自己的系统提示是会话开始时定的，不会因为 cwd 变了就重算；
        # 不主动说一声，它会照着主仓库路径汇报文件位置（实际落在 Worktree 里）
        self._announce(
            f"[Worktree] 你的文件操作现在落在独立工作目录：{path}"
            "（相对路径都相对它；报告文件位置时请用这个目录下的路径）"
        )

    def _leave_worktree(self) -> None:
        self.worktree_cwd = None
        if self.agent is not None:
            self.agent.cwd = os.getcwd()
        self._announce(f"[Worktree] 已退出，文件操作回到主工作目录：{os.getcwd()}")

    def _announce(self, text: str) -> None:
        """把一条说明挂到下一轮用户消息前（复用后台任务通知的通道）。"""
        self._pending_notices.append(text)

    # ------------------------------------------------------------ Team（ch15）

    async def run_team(self, sub: str, args: str) -> str:
        """`/team` 的实现：解析参数、调服务、返回文本。"""
        if self.team_service is None:
            return "Team 未启用（需要先选定 provider，且团队目录可写）"
        if sub == "create":
            return await self._team_create(args)
        if sub == "list":
            return self._team_list()
        if sub == "info":
            return self._team_info(args)
        if sub == "kill":
            return await self._team_kill(args)
        if sub == "delete":
            return await self._team_delete(args)
        return f"未知子命令：{sub}（可用：create / list / info / kill / delete）"

    async def _team_create(self, args: str) -> str:
        parts = args.split(maxsplit=1)
        if not parts:
            return "用法：/team create <名字> [描述]"
        name = parts[0]
        desc = parts[1] if len(parts) > 1 else ""
        try:
            team = await asyncio.to_thread(self.team_service.create_team, name, desc)
        except Exception as e:
            return f"创建团队失败：{e}"
        self._refresh_tools()
        return (
            f"已创建团队「{team.name}」（目录名 {team.sanitized_name}，后端 {team.backend}）\n"
            "协作工具已加入当前会话；用 Agent 工具带 team_name 派队员。"
        )

    def _team_list(self) -> str:
        teams = self.team_manager.list()
        if not teams:
            return "（无团队；用 /team create <名字> 创建）"
        current = self.team_service.active_team
        lines: list[str] = []
        for team in teams:
            active = sum(1 for m in team.members if m.is_active is not False)
            mark = (
                " *当前"
                if current and current.sanitized_name == team.sanitized_name
                else ""
            )
            lines.append(
                f"- {team.name}（{team.sanitized_name}）[后端 {team.backend}]{mark}\n"
                f"  成员 {len(team.members)}（活跃 {active}）"
            )
        return "\n".join(lines)

    def _team_info(self, args: str) -> str:
        name = args.strip()
        team = self.team_manager.get(name) if name else self.team_service.active_team
        if team is None:
            return f"未找到团队：{name or '（当前无团队）'}"
        lines = [f"团队「{team.name}」（{team.sanitized_name}）后端 {team.backend}"]
        if team.description:
            lines.append(f"目标：{team.description}")
        for member in team.members:
            state = "活跃" if member.is_active is not False else "空闲"
            lines.append(f"- {member.name} [{state}]")
            if member.agent_type:
                lines.append(f"  角色：{member.agent_type}")
            if member.branch:
                lines.append(f"  分支：{member.branch}")
            if member.worktree_path:
                lines.append(f"  工作目录：{member.worktree_path}")
            if member.plan_mode_required:
                lines.append("  需 Lead 审批计划")
        return "\n".join(lines)

    async def _team_kill(self, args: str) -> str:
        member = args.strip()
        if not member:
            return "用法：/team kill <成员>"
        try:
            ok = await self.team_service.kill(member)
        except Exception as e:
            return f"终止失败：{e}"
        return f"已终止成员 {member}" if ok else f"未找到成员：{member}"

    async def _team_delete(self, args: str) -> str:
        parts = args.split()
        force = "--force" in parts
        name = next((p for p in parts if not p.startswith("--")), "")
        if not name:
            return "用法：/team delete <名字> [--force]"
        try:
            report = await asyncio.to_thread(self.team_service.delete_team, name, force)
        except Exception as e:
            return f"删除团队失败：{e}"
        self._refresh_tools()
        removed = "、".join(report["removed_members"]) or "（无队员）"
        text = f"已删除团队「{report['team']}」，清理成员：{removed}"
        if report["warnings"]:
            text += "\n" + "\n".join(f"告警：{w}" for w in report["warnings"])
        return text

    def _parent_context(self) -> ParentContext:
        """把父侧能力打包给子 Agent 运行器。"""
        return ParentContext(
            provider=self.provider,
            provider_cfg=self.provider_cfg,
            engine=self.engine,
            hooks=self.hooks,
            version=__version__,
            registry_factory=self._base_registry,
            context_factory=self._build_context,
            conv_getter=lambda: self.conv,
            approval_handler=self._subagent_approval,
        )

    def _on_task_done(self, task) -> None:
        """后台任务完成：攒一条通知，下一次发送前注入主对话。"""
        notice = (
            "<task-notification>\n"
            f"后台任务 {task.id}（{task.name or '子 Agent'}）状态：{task.status}\n"
            f"结果：{task.result[:500]}\n"
            "</task-notification>"
        )
        self._pending_notices.append(notice)
        try:
            self.query_one("#log", RichLog).write(
                render_notice(f"后台任务 {task.id} {task.status}：{task.result[:60]}")
            )
        except Exception:
            self.log.debug("写后台任务日志失败", exc_info=True)

    async def _subagent_approval(self, call, reason) -> Outcome:
        """子 Agent / 队员的 Ask 升级到主 TUI：复用 APPROVING 弹窗，返回用户选择。

        两条保护：

        1. **串行**——同一时刻只弹一个。队员在后台跑时主 Agent 可能自己也在
           弹审批，两个请求抢同一块 `#streaming` 会把界面与 state 都搞乱。
        2. **等主对话不忙**——`prev_state` 必须在等完之后再取。早先直接取，
           队员在 STREAMING 期间弹窗、结束后把 state 还原成 STREAMING，
           主对话就再也不回 IDLE（按钮全失效）。等不到就按拒绝处理，
           绝不把队员无限期挂住。
        """
        delay = 0.1
        remaining = _APPROVAL_WAIT_SECONDS
        async with self._approval_lock:
            busy_states = (
                SessionState.STREAMING,
                SessionState.APPROVING,
                SessionState.SELECTING,
                SessionState.RESUMING,
                SessionState.COMPLETING,
            )
            while self.state in busy_states:
                if remaining <= 0:
                    self.log.debug("子 Agent 审批等待超时，按拒绝处理")
                    return Outcome.DENY_ONCE
                await asyncio.sleep(delay)
                remaining -= delay

            future = asyncio.get_event_loop().create_future()
            request = PermissionRequest(
                tool_call=call,
                friendly_name=friendly_name(call.name),
                preview=_preview_args(call.input),
                reason=f"[来自 SubAgent] {reason}",
                respond=future,
            )
            prev_state = self.state
            self._pending_request = request
            self._approve_cursor = 0
            self.state = SessionState.APPROVING
            self.set_focus(None)
            self.query_one("#streaming", Static).update(
                render_permission_request(request, 0)
            )
            try:
                return await future
            finally:
                self._pending_request = None
                self.state = prev_state

    def _setup_agent(self) -> None:
        """provider 确定后，构造权限引擎与 Agent。

        注册顺序固定：内置 6 个在前，MCP 工具在后（已按 server/name 排序），
        load_skill 最后。tools 段位于 system 之前，顺序一变 prompt 缓存前缀就全失效。
        """
        self.engine, _ = new_engine(os.getcwd())
        # 与 self.conv 同生命周期：跨轮持有锚点与熔断状态。
        # 挂在 Agent 上也等价，但 Agent 一旦被重建（切 provider / 重载工具）
        # 就会静默丢掉熔断状态，连续失败的摘要会重新烧钱。
        self.context = self._build_context()

        # ch13：后台任务表 + 子 Agent 运行器（必须在 _build_agent 之前，工具池要用）
        self.task_manager = TaskManager(on_done=self._on_task_done)
        self.subagent_runner = None
        if self.catalog is not None:
            self.subagent_runner = SubAgentRunner(
                self.catalog,
                self.task_manager,
                self._parent_context(),
                worktrees=self.worktrees,
            )
        # ch15：团队服务要等 runner 就位（它复用 runner 建队员 Agent）
        self.team_service = None
        if self.team_manager is not None and self.subagent_runner is not None:
            self.team_service = TeamService(
                self.team_manager,
                self.subagent_runner,
                mode_getter=lambda: self.mode,
            )

        self.agent = self._build_agent(
            on_turn_end=self._note_hook, context=self.context
        )

        # AC41：新建会话时 writer 是在 cli 里建的，那时还没选 provider，model 是空串。
        # 现在 provider 定了，回填——否则整个新会话的每一行都没有 model 字段。
        if self.writer is not None:
            self.writer.set_model(self.provider.model)

        # ch09：恢复的会话如果已经贴着窗口，先压一次再让用户输入，
        # 否则第一轮请求必然撞 400（这时还没吐任何字，压缩是安全的）。
        #
        # 这里只能建 task 不能 await（`_setup_agent` 是同步的，且压缩要调 LLM）。
        # 但**必须把 task 存下来**：`submit()` 在第一轮请求前会 await 它。
        # 早先的版本直接 `create_task(...)` 丢掉引用——用户手快就能在压缩完成前
        # 提交，那时历史仍是压缩前的，第一轮照样撞 400，AC16 只是看起来成立。
        if (
            self._restore is not None
            and self._restore.messages
            and self.context.should_compress(self.conv)
        ):
            self._restore_compress_task = asyncio.create_task(
                self._compress_after_restore()
            )

    async def _await_restore_compress(self) -> None:
        """等「恢复后压缩」收尾。最多等一次，异常不外抛。"""
        task, self._restore_compress_task = self._restore_compress_task, None
        if task is None:
            return
        try:
            await task
        except Exception as exc:  # 压缩失败不该拦住对话：后续还有 ch08 的撞墙路径
            self.query_one("#log", RichLog).write(
                f"（恢复后压缩失败：{exc.__class__.__name__}）"
            )

    async def _compress_after_restore(self) -> None:
        log = self.query_one("#log", RichLog)
        report = await self.agent.compact_now(self.conv)
        if report.notice:
            log.write(report.notice)

    async def _note_hook(self, conv: Conversation) -> None:
        """每轮自然停下后的沉淀入口。

        异常全吞：笔记失败不该在 UI 上弹任何东西——用户没主动要求记忆，
        不该因为记忆坏了而看到报错。
        """
        if self.provider is None:
            return
        try:
            await notes.update(
                self.provider,
                conv.messages(),
                self._memory,
                _home_memory_dir(),
                Path(os.getcwd()) / ".helpcode" / "memory",
            )
        except Exception as exc:  # 兜底，连 notes 自己的兜底都漏了才算
            log = self.query_one("#log", RichLog)
            log.write(f"（笔记更新跳过：{exc.__class__.__name__}）")

    # ------------------------------------------------------------ Skill

    def _register_skill_command(self, skill) -> None:
        """Skill 激活后注册斜杠短命令；与内置/已注册命令撞名则跳过并提示。"""
        if self.commands is None:
            return
        try:
            self.commands.register(make_skill_command(skill))
        except DuplicateCommandError:
            self.query_one("#log", RichLog).write(
                render_notice(
                    f"/{skill.name} 与已有命令冲突，未注册短命令"
                    f"（仍可用 load_skill 加载）"
                )
            )

    async def _run_skill(self, name: str, args: str) -> str:
        """按 Skill 执行模式跑：shared 送对话，isolated 独立跑完回流摘要。"""
        if self.skill_manager is None:
            return f"未找到 Skill：{name}"
        skill = self.skill_manager.get(name)
        if skill is None:
            return f"未找到 Skill：{name}（输入 /skills 查看可用 Skill）"
        self.skill_manager.activate(skill.name)
        self._register_skill_command(skill)
        if skill.mode is ExecutionMode.SHARED:
            await self.send_to_ai(substitute(skill.body, args))
            return ""
        return await self._run_skill_isolated(skill, args)

    async def _run_skill_isolated(self, skill, args: str) -> str:
        """独立对话跑完 Skill，把摘要回流主历史；期间锁住输入。"""
        self.state = SessionState.STREAMING
        try:
            seed = _seed_messages(self.conv.messages(), skill.history)
            iso = Conversation.from_messages(seed)
            # 独立 Agent：不共享 ContextManager（锚点隔离），不触发笔记（on_turn_end=None）
            iso_agent = self._build_agent(on_turn_end=None)
            text = ""
            async for ev in iso_agent.run(
                substitute(skill.body, args),
                iso,
                asyncio.Event(),
                Mode.DEFAULT,
            ):
                if ev.err is not None:
                    text = f"（执行出错：{ev.err.__class__.__name__}）"
                    break
                if ev.text:
                    text += ev.text
                if ev.done:
                    break
            summary = _last_assistant(iso) or text or "（无输出）"
            self.conv.add_assistant(f"[Skill: {skill.name}]\n{summary}")
            return f"[Skill: {skill.name}] 完成\n{summary}"
        finally:
            self.state = SessionState.IDLE

    def _apply_resume(self, meta) -> None:
        """切换到选中的会话：历史、存档、落盘目录三者一起换。"""
        from ..memory.restore import restore as restore_session

        # ch12：离开旧会话（同步方法里异步触发）
        if self.hooks is not None:
            old_id = self.session_id or ""
            asyncio.create_task(
                self._fire_hook(HookEvent.SESSION_END, {"session_id": old_id})
            )

        report = restore_session(meta.jsonl)
        self.conv = Conversation.from_messages(
            report.messages,
            on_append=self._writer_append,
            on_replace=self._writer_replace,
        )
        self.writer = _open_writer(
            meta.jsonl, self.provider.model if self.provider else ""
        )
        self.session_id = meta.session_id
        if self.provider is not None:
            self._setup_agent()  # 重建 ContextManager，让 spill 指向新会话目录
        if self.hooks is not None:
            self.hooks.reset_session()
            asyncio.create_task(
                self._fire_hook(
                    HookEvent.SESSION_RESUME, {"session_id": meta.session_id}
                )
            )
        self.query_one("#log", RichLog).write(
            render_restored(meta.session_id, len(report.messages))
        )

    def _writer_append(self, msg) -> None:
        if self.writer is not None:
            self.writer.append(msg)

    def _writer_replace(self, msgs) -> None:
        if self.writer is not None:
            self.writer.write_compact_marker()
            self.writer.append_all(msgs)

    async def submit(self, text: str) -> None:
        """处理一次输入提交：命令走本地分发，否则送进 AI。"""
        if self.state != SessionState.IDLE:
            return  # 流式/待批准/选择期间不接受新的提交
        text = text.strip()
        if not text:
            return

        # AC16：恢复后那次压缩必须先落地，命令与普通提问都会读历史。
        await self._await_restore_compress()

        parsed = parse(text)
        if parsed is None:
            # ch12：UserPromptSubmit——可拦截（原因回显、消息不入历史）或注入
            res = await self._fire_hook(HookEvent.USER_PROMPT_SUBMIT, {"prompt": text})
            if res is not None and res.block_reason:
                self.query_one("#log", RichLog).write(
                    render_notice(f"提交被 Hook 拦截：{res.block_reason}")
                )
                return
            if res is not None and res.injected:
                text = "\n\n".join([*res.injected, text])
            await self.send_to_ai(text)
            return
        await self.dispatch_command(parsed)

    async def dispatch_command(self, parsed: ParsedCommand) -> None:
        """把解析结果分发给命令；未命中或命令抛错都不进 AI、不崩溃。"""
        registry = self.commands
        if registry is None:
            registry = build_default_registry()
        cmd = registry.resolve(parsed.name)
        log = self.query_one("#log", RichLog)
        if cmd is None:
            log.write(
                render_notice(f"未知命令 /{parsed.name}，输入 /help 查看全部命令")
            )
            return
        try:
            await cmd.handler(
                CommandContext(cmd, parsed.args, TextualHost(self), registry)
            )
        except Exception as exc:
            log.write(render_notice(f"/{cmd.name} 执行失败：{exc.__class__.__name__}"))

    async def send_to_ai(self, text: str) -> None:
        """把一条用户消息送进对话循环（非命令路径的唯一入口）。"""
        # ch13：后台任务完成通知随下一轮带进主对话
        if self._pending_notices:
            text = "\n\n".join([*self._pending_notices, text])
            self._pending_notices = []
        self.cancel_event = asyncio.Event()  # 每次提交重置取消信号
        log = self.query_one("#log", RichLog)
        log.write(user_block(text))
        self.query_one("#input", InputArea).clear()
        self.cur_reply = ""
        self._cur_iteration = 0
        self.turn_start = time.monotonic()
        self.state = SessionState.STREAMING
        self._stream_task = asyncio.create_task(self._consume_stream(text))
        self._timer = self.set_interval(0.1, self._tick)

    async def start_new_session(self) -> str | None:
        """`/clear`：新建会话目录与存档，重建 Agent 让落盘指向新目录。"""
        from ..memory import session as session_mod

        # ch12：关闭旧会话 → 重置 only_once → 新会话开始
        await self._fire_hook(
            HookEvent.SESSION_END, {"session_id": self.session_id or ""}
        )
        if self.hooks is not None:
            self.hooks.reset_session()

        # F14：清空对话顺带清掉已激活的 Skill（SOP 不再钉入）
        if self.skill_manager is not None:
            self.skill_manager.clear()

        try:
            sid = session_mod.new_session_id()
            session_mod.ensure_session_dir(Path(os.getcwd()), sid)
        except OSError:
            return None
        jsonl = session_mod.jsonl_path(Path(os.getcwd()), sid)
        self.session_id = sid
        self.writer = _open_writer(jsonl, self.provider.model if self.provider else "")
        self.conv = Conversation(
            on_append=self._writer_append,
            on_replace=self._writer_replace,
        )
        if self.provider is not None:
            self._setup_agent()  # 重建 ContextManager，spill 指向新会话目录
        await self._fire_hook(
            HookEvent.SESSION_START,
            {"session_id": sid, "cwd": os.getcwd()},
        )
        return sid

    def handle_tab(self) -> None:
        """输入框按 Tab：命令补全；非斜杠输入不劫持。"""
        registry = self.commands or build_default_registry()
        input_widget = self.query_one("#input", InputArea)
        text = input_widget.text
        if not text.startswith("/"):
            return
        candidates = registry.complete(text)
        log = self.query_one("#log", RichLog)
        if not candidates:
            log.write(render_notice("没有匹配的命令，输入 /help 查看全部"))
            return
        if len(candidates) == 1:
            input_widget.text = candidates[0] + " "
            input_widget.move_cursor((0, len(input_widget.text)))
            return
        self._show_command_menu(candidates)

    def _show_command_menu(self, candidates: list[str]) -> None:
        """多匹配：弹出候选菜单。"""
        menu = self.query_one("#command_list", OptionList)
        menu.clear_options()
        menu.add_options([Option(c) for c in candidates])
        menu.display = True
        self.state = SessionState.COMPLETING
        menu.focus()

    def _select_command(self, option_index: int) -> None:
        """菜单里选中一条命令 → 填入输入框并回 IDLE。"""
        menu = self.query_one("#command_list", OptionList)
        choice = menu.get_option_at_index(option_index)
        input_widget = self.query_one("#input", InputArea)
        if choice is not None:
            input_widget.text = str(choice.prompt) + " "
            input_widget.move_cursor((0, len(input_widget.text)))
        self._close_command_menu()

    def _close_command_menu(self) -> None:
        self.query_one("#command_list", OptionList).display = False
        self.state = SessionState.IDLE
        self.query_one("#input", InputArea).focus()

    def action_cycle_mode(self) -> None:
        """Shift+Tab：循环切换权限模式（仅 IDLE 生效）。"""
        if self.state != SessionState.IDLE:
            return
        self.mode = Mode((int(self.mode) + 1) % 4)
        self._update_statusbar()
        log = self.query_one("#log", RichLog)
        log.write(user_block(f"已切换到权限模式: {self.mode}"))

    def on_key(self, event) -> None:
        """APPROVING 态拦截选择按键；IDLE 态 shift+tab 切换模式。"""
        if self.state == SessionState.APPROVING:
            key = event.key
            if key in ("up", "k"):
                self._approve_cursor = (self._approve_cursor - 1) % 3
                event.stop()
                self._refresh_approval()
            elif key in ("down", "j"):
                self._approve_cursor = (self._approve_cursor + 1) % 3
                event.stop()
                self._refresh_approval()
            elif key == "enter":
                event.stop()
                self._resolve_approval(self._outcome_for_index(self._approve_cursor))
            elif key in ("1", "2", "3"):
                event.stop()
                self._resolve_approval(self._outcome_for_index(int(key) - 1))
            elif key == "y":
                event.stop()
                self._resolve_approval(Outcome.ALLOW_ONCE)
            elif key in ("n", "d"):
                event.stop()
                self._resolve_approval(Outcome.DENY_ONCE)
            return
        if event.key == "shift+tab":
            event.stop()
            event.prevent_default()
            self.action_cycle_mode()

    def _outcome_for_index(self, idx: int) -> Outcome:
        return [Outcome.ALLOW_ONCE, Outcome.ALLOW_FOREVER, Outcome.DENY_ONCE][idx]

    def _refresh_approval(self) -> None:
        if self._pending_request is not None:
            self.query_one("#streaming", Static).update(
                render_permission_request(self._pending_request, self._approve_cursor)
            )

    def _resolve_approval(self, outcome: Outcome) -> None:
        """回填用户决策，恢复 STREAMING 让 agent 继续。"""
        if (
            self._pending_request is not None
            and not self._pending_request.respond.done()
        ):
            self._pending_request.respond.set_result(outcome)
        self._pending_request = None
        self.query_one("#streaming", Static).update("")
        self.state = SessionState.STREAMING
        self.query_one("#input", InputArea).focus()

    async def action_cancel(self) -> None:
        """Esc：选会话/补全/流式/待批准态各自取消当前动作（不退出程序）。"""
        if self.state == SessionState.RESUMING:
            self._exit_resume()
            return
        if self.state == SessionState.COMPLETING:
            self._close_command_menu()
            return
        if self.state == SessionState.APPROVING:
            # 先 resolve future 解开 agent 等待，再置取消信号
            if (
                self._pending_request is not None
                and not self._pending_request.respond.done()
            ):
                self._pending_request.respond.set_result(Outcome.DENY_ONCE)
            self._pending_request = None
            self.query_one("#streaming", Static).update("")
            self.state = SessionState.STREAMING
            self.cancel_event.set()
        elif self.state == SessionState.STREAMING:
            self.cancel_event.set()

    async def action_quit(self) -> None:
        """Ctrl+C：流式/待批准态取消、空闲态退出。"""
        if self.state in (SessionState.STREAMING, SessionState.APPROVING):
            await self.action_cancel()
        else:
            # 退出前等沉淀任务收尾，再关文件——否则写了一半的笔记会丢
            await self._await_restore_compress()  # 压缩也在写 jsonl，先等它
            if self.agent is not None:
                await self.agent.drain_note_tasks()
            # ch12：会话结束（进程关闭前）
            await self._fire_hook(
                HookEvent.SESSION_END, {"session_id": self.session_id or ""}
            )
            if self.hooks is not None:
                await self.hooks.drain()
            if self.writer is not None:
                self.writer.close()
            self.exit()

    def on_option_list_option_selected(self, event) -> None:
        """三个 OptionList 共用同一个事件：按 id 分流。

        注意：Textual 8 会沿 MRO 逐个调用同名处理器，SelectMixin 的同名方法
        也会被调用一次。所以这里**只**处理 resume / command 两种列表，
        其余（provider_list）直接返回，交给 SelectMixin 自己处理——不能再
        调 `super()`，否则 provider 选择会被执行两次。
        """
        oid = getattr(event.option_list, "id", None)
        if oid == "resume_list":
            self._select_session(event.option_index)
            return
        if oid == "command_list":
            self._select_command(event.option_index)
            return


def _home_memory_dir() -> Path:
    return Path.home() / ".helpcode" / "memory"


def _open_writer(jsonl: Path, model: str):
    from ..memory.session import SessionWriter

    return SessionWriter(jsonl, model)


def _seed_messages(messages, n: int) -> list:
    """独立对话的种子：只带 user/assistant 有正文的消息，最多 n 条。

    tool 消息不能带：它们的 tool_call_id 指向的调用不在种子历史里，孤儿化会
    触发协议错误。assistant 里纯工具请求（content 为空）同理要排除。
    """
    if n <= 0:
        return []
    keep = [m for m in messages if m.role in ("user", "assistant") and m.content]
    return keep[-n:]


def _last_assistant(conv: Conversation) -> str:
    """独立对话的最后一条 assistant 正文，作为回流摘要。"""
    for m in reversed(conv.messages()):
        if m.role == "assistant" and m.content:
            return m.content
    return ""


def _preview_args(input: dict) -> str:
    """工具入参的一句预览（审批弹窗用）。"""
    for key in ("path", "command", "pattern", "prompt"):
        if key in input:
            s = str(input[key])
            return s if len(s) <= 40 else s[:40] + "…"
    return ""
