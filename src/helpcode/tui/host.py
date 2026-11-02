"""CommandHost 的 Textual 实现，把 HelpCodeApp 的能力包装成协议。"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from textual.widgets import RichLog

from .. import __version__
from ..command.host import HostSnapshot, MemoryOverview
from ..permission import Mode
from ..prompt import render_banner
from ..tui.view import error_block

if TYPE_CHECKING:
    from .app import HelpCodeApp

_LAYER_LABELS = (
    ("AGENTS.md", "项目根"),
    (".helpcode/AGENTS.md", "项目配置"),
    ("~/.helpcode/AGENTS.md", "用户目录"),
)


class TextualHost:
    """让命令通过统一协议操作 TUI，而不必 import 任何控件。"""

    def __init__(self, app: HelpCodeApp) -> None:
        self._app = app

    # ------------------------------------------------------------ 只读

    def snapshot(self) -> HostSnapshot:
        app = self._app
        memory = MemoryOverview(
            layers=self._layers(),
            index_lines=self._index_lines(),
            note_count=self._note_count(),
            warnings=self._warnings(),
        )
        used = app.context.estimate(app.conv) if app.context else 0
        window = app.context.context_window if app.context else 0
        return HostSnapshot(
            model=app.provider.model if app.provider else "",
            session_id=app.session_id,
            message_count=len(app.conv.messages()),
            context_window=window,
            context_used=used,
            mode=app.mode,
            usage=app.last_usage,
            sessions=tuple(app.sessions or ()),
            memory=memory,
        )

    def _layers(self) -> tuple[tuple[str, bool], ...]:
        app = self._app
        present = (
            {layer.label for layer in app._instruction_set.layers}
            if app._instruction_set is not None
            else set()
        )
        return tuple((label, label in present) for label, _desc in _LAYER_LABELS)

    def _index_lines(self) -> tuple[tuple[str, int], ...]:
        app = self._app
        bundle = app._index_bundle
        if bundle is None:
            return ()
        return (
            ("用户级", len(bundle.user.splitlines())),
            ("项目级", len(bundle.project.splitlines())),
        )

    def _note_count(self) -> int:
        mem = Path(os.getcwd()) / ".helpcode" / "memory"
        if not mem.is_dir():
            return 0
        return sum(
            1 for p in mem.iterdir() if p.suffix == ".md" and p.name != "index.md"
        )

    def _warnings(self) -> tuple[str, ...]:
        app = self._app
        out: list[str] = []
        if app._instruction_set is not None:
            out += app._instruction_set.warnings
        if app._index_bundle is not None:
            out += app._index_bundle.warnings
        return tuple(out)

    # ------------------------------------------------------------ 输出

    def show(self, text: str) -> None:
        self._app.query_one("#log", RichLog).write(text)

    def show_error(self, text: str) -> None:
        self._app.query_one("#log", RichLog).write(error_block(RuntimeError(text)))

    def clear_screen(self) -> None:
        log = self._app.query_one("#log", RichLog)
        log.clear()
        log.write(render_banner(__version__, os.getcwd()))

    # ------------------------------------------------------------ 动作

    async def send_prompt(self, text: str) -> None:
        await self._app.send_to_ai(text)

    async def run_skill(self, name: str, args: str) -> str:
        return await self._app._run_skill(name, args)

    async def compact(self) -> str:
        report = await self._app.agent.compact_now(self._app.conv)
        return report.notice

    def get_mode(self) -> Mode:
        return self._app.mode

    def set_mode(self, mode: Mode) -> None:
        self._app.mode = mode

    def refresh_status(self) -> None:
        self._app._update_statusbar()

    async def start_new_session(self) -> str | None:
        return await self._app.start_new_session()

    def open_session_picker(self) -> None:
        self._app._enter_resume()

    async def worktree(self, sub: str, args: str) -> str:
        return await self._app.run_worktree(sub, args)

    async def team(self, sub: str, args: str) -> str:
        return await self._app.run_team(sub, args)

    def sandbox(self, sub: str, args: str) -> str:
        """OS 级沙箱开关。

        三档模式对应两种诉求：``on`` 是「我信任沙箱，别每条命令都问我」，
        ``confirm`` 是「我要隔离，但关键命令仍想看一眼」。两者的差别只在
        权限层是否自动放行，隔离强度完全一样。
        """
        engine = getattr(self._app, "engine", None)
        if engine is None:
            return "权限引擎尚未就绪，稍后再试。"
        state = engine.sandbox

        if sub == "status":
            return self._sandbox_report(state)

        if sub == "net":
            want = args.strip().lower()
            if want not in ("on", "off"):
                return "用法：/sandbox net on|off"
            state.config = replace(state.config, network_enabled=want == "on")
            return self._sandbox_report(state)

        if sub == "off":
            state.enabled = False
            state.auto_allow = False
            return "沙箱已关闭，bash 恢复无隔离执行。\n\n" + self._sandbox_report(state)

        # on / confirm：先确认后端真的可用，不能"显示开启但实际没有"
        backend = state.backend
        if backend is None:
            return (
                "当前平台没有对等的沙箱机制（Windows 无 sandbox-exec / bwrap），"
                "无法开启。\n"
                "文件类工具仍受路径围栏保护，但 bash 子进程不受限制。"
            )
        if not backend.available():
            hint = (
                "apt install bubblewrap"
                if backend.name == "bwrap"
                else "需要 macOS 自带的 /usr/bin/sandbox-exec"
            )
            return f"后端 {backend.name} 不可用（{hint}），沙箱未开启。"

        state.enabled = True
        state.auto_allow = sub == "on"
        return self._sandbox_report(state)

    def _sandbox_report(self, state) -> str:
        cfg = state.config
        allow = "\n".join(f"  · {p}" for p in cfg.allow_write) or "  （无）"
        deny = "\n".join(f"  · {p}" for p in cfg.deny_write) or "  （无）"
        head = (
            "沙箱只包 bash 子进程；read_file / write_file 等文件工具走的是"
            "应用层路径围栏，不受这里影响。"
        )
        return (
            f"OS 沙箱：{state.describe()}\n\n"
            f"允许写入：\n{allow}\n\n"
            f"强制只读（优先于允许）：\n{deny}\n\n"
            f"{head}"
        )

    async def quit(self) -> None:
        await self._app.action_quit()
