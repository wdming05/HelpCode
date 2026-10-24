"""TUI App：HelpCodeApp、状态机与布局。"""

import asyncio
import os
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from .. import __version__
from ..agent import Agent
from ..config import DEFAULT_WINDOW, ProviderConfig
from ..context import ContextManager
from ..conversation import Conversation
from ..llm import Provider, new_provider
from ..memory import notes
from ..memory.session import tool_results_dir
from ..permission import Mode, Outcome, new_engine
from ..prompt import EXECUTE_DIRECTIVE, render_banner
from ..tool import new_default_registry
from . import SessionState
from .resume import ResumeMixin
from .select import SelectMixin
from .stream import StreamMixin
from .view import (
    render_cleanup,
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
        """拦截 Enter（提交）与 Alt+Enter（换行），其余交给 TextArea 默认处理。"""
        key = event.key
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.action_submit_input()
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

    def _update_statusbar(self) -> None:
        """更新底部状态栏：左权限模式名、右模型名。"""
        if self.provider is None:
            return
        self.query_one("#statusbar", Static).update(
            render_statusbar(self.mode, self.provider.model)
        )

    def _setup_agent(self) -> None:
        """provider 确定后，构造权限引擎与 Agent。

        注册顺序固定：内置 6 个在前，MCP 工具在后（已按 server/name 排序）。
        tools 段位于 system 之前，顺序一变 prompt 缓存前缀就全失效。
        """
        self.engine, _ = new_engine(os.getcwd())
        registry = new_default_registry()
        for tool in self.mcp_tools:
            registry.register(tool)
        # 与 self.conv 同生命周期：跨轮持有锚点与熔断状态。
        # 挂在 Agent 上也等价，但 Agent 一旦被重建（切 provider / 重载工具）
        # 就会静默丢掉熔断状态，连续失败的摘要会重新烧钱。
        #
        # 窗口值取自配置对象而非 provider：provider 只该管"怎么发请求"，
        # 不该承载业务参数（否则改一个字段就要动协议层）。
        cfg = self.provider_cfg
        if cfg is not None and cfg.context_window:
            window = cfg.context_window
        else:  # 手工构造的 provider（测试）没有配置对象，按 openai 协议默认
            window = DEFAULT_WINDOW[cfg.protocol if cfg else "openai"]
        self.context = ContextManager(
            self.provider,
            Path(os.getcwd()),
            window,
            spill_subdir=tool_results_dir(Path(os.getcwd()), self.session_id),
        )
        self.agent = Agent(
            self.provider,
            registry,
            __version__,
            self.engine,
            self.context,
            system_extra=(self._instructions, self._memory),
            on_turn_end=self._note_hook,
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

    def _apply_resume(self, meta) -> None:
        """切换到选中的会话：历史、存档、落盘目录三者一起换。"""
        from ..memory.restore import restore as restore_session

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
        """处理一次输入提交。"""
        if self.state != SessionState.IDLE:
            return  # 流式/待批准/选择期间不接受新的提交
        text = text.strip()
        if not text:
            return
        if text == "/exit":
            await self.action_quit()
            return

        # AC16：恢复后那次压缩必须先落地。放在 `/exit` 之后、所有真正用到历史的
        # 分支之前——`/compact`、`/resume`、普通提问都会读 `self.conv`。
        await self._await_restore_compress()

        if text == "/plan":
            self.mode = Mode.PLAN
            self._update_statusbar()
            log = self.query_one("#log", RichLog)
            log.write(user_block("/plan"))
            log.write("已进入计划模式（仅只读工具，可 /do 切换回执行）")
            return
        if text == "/do":
            self.mode = Mode.DEFAULT
            self._update_statusbar()
            text = EXECUTE_DIRECTIVE  # 注入执行指令，触发模型按计划执行
        if text == "/compact":
            log = self.query_one("#log", RichLog)
            log.write(user_block("/compact"))
            report = await self.agent.compact_now(self.conv)
            log.write(report.notice or "无需压缩")
            return
        if text == "/resume":
            self.query_one("#log", RichLog).write(user_block("/resume"))
            self._enter_resume()
            return

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
        """Esc：选会话/流式/待批准态各自取消当前动作（不退出程序）。"""
        if self.state == SessionState.RESUMING:
            self._exit_resume()
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
            if self.writer is not None:
                self.writer.close()
            self.exit()

    def on_option_list_option_selected(self, event) -> None:
        """两个 OptionList 共用一个事件：按 id 分派。"""
        if getattr(event.option_list, "id", None) == "resume_list":
            self._select_session(event.option_index)
            return
        # 否则是 provider 选择（沿用 SelectMixin 的行为）
        super().on_option_list_option_selected(event)


def _home_memory_dir() -> Path:
    return Path.home() / ".helpcode" / "memory"


def _open_writer(jsonl: Path, model: str):
    from ..memory.session import SessionWriter

    return SessionWriter(jsonl, model)
