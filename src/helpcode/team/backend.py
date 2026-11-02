"""执行后端：队员跑在哪里。

三种：tmux 窗格 / iTerm2 分屏 / 同进程协程。检测**只做一次**，选定后不再
回退——静默回退会让人以为「队员在独立终端里跑」，实际却在主进程里抢事件
循环，排查时完全看不出来。
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# 窗格后端启动命令的超时（秒）：只是起进程，不该等太久
SPAWN_TIMEOUT = 20.0


class BackendType(enum.Enum):
    TMUX = "tmux"
    ITERM2 = "iterm2"
    IN_PROCESS = "in-process"


class BackendError(RuntimeError):
    """窗格后端启动/唤醒/终止失败。**不**回退到 in-process。"""


@dataclass
class SpawnRequest:
    """启动一个队员需要的最小上下文。"""

    team: str
    member: str
    prompt: str
    worktree: str
    session_dir: str
    model: str = ""
    agent_type: str = ""
    plan_mode_required: bool = False
    # 跨进程 trace 关联：lead 派生的子 trace id（传给 worker 作为其 trace_id）
    trace_parent: str = ""
    # in-process 后端用：真正启动 Agent 的回调，返回 (agent_id, cancel)
    launcher: (
        Callable[[SpawnRequest], Awaitable[tuple[str, Callable | None]]] | None
    ) = None
    extra: dict = field(default_factory=dict)


class Backend(Protocol):
    def type(self) -> BackendType: ...

    async def spawn(self, req: SpawnRequest) -> tuple[str, str]: ...

    async def wake(self, pane_id: str, agent_id: str) -> None: ...

    async def kill(self, pane_id: str, agent_id: str) -> None: ...


def env_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def detect_backend(
    env: dict | None = None, which: Callable[[str], str | None] = shutil.which
) -> BackendType:
    """按优先级一次性决定后端。

    顺序：已在 tmux 里 → 用 tmux（分屏最自然）；iTerm2 + it2 → iterm2；
    装了 tmux → tmux；都不行 → in-process。Windows 上通常落到最后一种。
    """
    env = env if env is not None else os.environ
    # $TMUX 里放的是 socket 路径（不是 "1"），所以判「非空」而不是判真值字面量
    if (env.get("TMUX") or "").strip():
        return BackendType.TMUX
    if env.get("TERM_PROGRAM") == "iTerm.app" and which("it2"):
        return BackendType.ITERM2
    if which("tmux"):
        return BackendType.TMUX
    return BackendType.IN_PROCESS


def make_backend(kind: BackendType) -> Backend:
    if kind is BackendType.TMUX:
        return PaneBackend(BackendType.TMUX)
    if kind is BackendType.ITERM2:
        return PaneBackend(BackendType.ITERM2)
    return InProcessBackend()


def build_member_command(req: SpawnRequest) -> list[str]:
    """窗格里跑的完整命令行。

    任务正文写进文件而不是塞进命令行：正文里有引号、换行、中文，
    拼进 shell 命令必然出事。
    """
    prompt_file = Path(req.session_dir) / "task.txt"
    cmd = [
        sys.executable,
        "-m",
        "helpcode.team.worker",
        "--team",
        req.team,
        "--member",
        req.member,
        "--worktree",
        req.worktree,
        "--session-dir",
        req.session_dir,
        "--prompt-file",
        str(prompt_file),
    ]
    if req.model:
        cmd += ["--model", req.model]
    if req.plan_mode_required:
        cmd.append("--plan-mode")
    if req.trace_parent:
        cmd += ["--trace-parent", req.trace_parent]
    return cmd


class InProcessBackend:
    """同进程协程：轻量，共享事件循环与 provider 连接。"""

    def __init__(self) -> None:
        self._cancels: dict[str, Callable | None] = {}

    def type(self) -> BackendType:
        return BackendType.IN_PROCESS

    async def spawn(self, req: SpawnRequest) -> tuple[str, str]:
        if req.launcher is None:
            raise BackendError("in-process 后端缺少 launcher，无法启动队员")
        agent_id, cancel = await req.launcher(req)
        self._cancels[agent_id] = cancel
        return "", agent_id

    async def wake(self, pane_id: str, agent_id: str) -> None:
        # 同进程不需要唤醒：队员每轮循环开头自己会去读邮箱
        return None

    async def kill(self, pane_id: str, agent_id: str) -> None:
        cancel = self._cancels.pop(agent_id, None)
        if cancel is not None:
            cancel()


class PaneBackend:
    """窗格后端：tmux split-window 或 it2 split，再用 send-keys 起进程。"""

    def __init__(self, kind: BackendType) -> None:
        self._kind = kind
        self._counter = 0

    def type(self) -> BackendType:
        return self._kind

    async def spawn(self, req: SpawnRequest) -> tuple[str, str]:
        prompt_file = Path(req.session_dir) / "task.txt"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(req.prompt, encoding="utf-8")

        command = build_member_command(req)
        if self._kind is BackendType.TMUX:
            pane_id = self._spawn_tmux(command)
        else:
            pane_id = self._spawn_iterm2(command)
        # 窗格里的成员是独立进程，agent id 由 Lead 侧生成后回填
        self._counter += 1
        return pane_id, f"pane-{self._counter}"

    async def wake(self, pane_id: str, agent_id: str) -> None:
        if self._kind is not BackendType.TMUX or not pane_id:
            return
        # tmux 里发一个空回车即可让读 stdin 的进程继续；常驻监听不在本章范围
        self._run(["tmux", "send-keys", "-t", pane_id, "", "Enter"])

    async def kill(self, pane_id: str, agent_id: str) -> None:
        if not pane_id:
            return
        if self._kind is BackendType.TMUX:
            self._run(["tmux", "kill-pane", "-t", pane_id])
        else:
            self._run(["it2", "session", "close", "-s", pane_id])

    # ------------------------------------------------------------ 内部

    def _spawn_tmux(self, command: list[str]) -> str:
        proc = self._run(
            [
                "tmux",
                "split-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-c",
                os.getcwd(),
            ]
        )
        pane_id = (proc.stdout or "").strip()
        if not pane_id:
            raise BackendError("tmux 未返回 pane id")
        quoted = " ".join(_quote(part) for part in command)
        self._run(["tmux", "send-keys", "-t", pane_id, quoted, "Enter"])
        return pane_id

    def _spawn_iterm2(self, command: list[str]) -> str:
        quoted = " ".join(_quote(part) for part in command)
        self._run(["it2", "split", "--command", quoted])
        self._counter += 1
        return f"iterm-{self._counter}"

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=SPAWN_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BackendError(f"{argv[0]} 调用失败：{e}") from e
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:200]
            raise BackendError(f"{argv[0]} 退出码 {proc.returncode}：{detail}")
        return proc


def _quote(text: str) -> str:
    """极简引号包裹：只处理含空格的参数（路径、prompt 文件）。"""
    if not text or any(ch in text for ch in " \"'\\$"):
        return '"' + text.replace('"', '\\"') + '"'
    return text


async def run_inline_launcher(req: SpawnRequest) -> tuple[str, Callable | None]:
    """默认 launcher 的占位：由 TeamService 注入真正的实现。"""
    await asyncio.sleep(0)
    raise BackendError("未注入 launcher")
