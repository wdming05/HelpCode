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
from ..tool import new_default_registry
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
        """主 Agent 的工具池 = 基础工具池 + Agent 工具。"""
        registry = self._base_registry()
        if self.subagent_runner is not None:
            registry.register(AgentTool(self.subagent_runner))
        return registry

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
        )

    async def _fire_hook(self, event: HookEvent, payload: dict):
        """触发 Hook；没装配引擎时返回 None。"""
        if self.hooks is None:
            return None
        return await self.hooks.dispatch(event, payload)

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
        """子 Agent 的 Ask 升级到主 TUI：复用 APPROVING 弹窗，返回用户选择。"""
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
                self.catalog, self.task_manager, self._parent_context()
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
