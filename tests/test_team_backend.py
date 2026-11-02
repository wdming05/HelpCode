"""后端：检测优先级、in-process 启动、窗格命令拼装、失败不回退。"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from helpcode.team.backend import (
    BackendError,
    BackendType,
    InProcessBackend,
    PaneBackend,
    SpawnRequest,
    build_member_command,
    detect_backend,
    make_backend,
)
from helpcode.team.worker import parse_args


def _req(tmp_path, **kw):
    data = {
        "team": "t",
        "member": "alice",
        "prompt": "写点东西",
        "worktree": str(tmp_path / "wt"),
        "session_dir": str(tmp_path / "sess"),
        "model": "",
        "agent_type": "explore",
        "plan_mode_required": False,
    }
    data.update(kw)
    return SpawnRequest(**data)


def test_detect_priority():
    # 已在 tmux 里 → tmux（哪怕 iTerm2 也开着）
    env = {"TMUX": "/tmp/tmux-1000/default,123,0", "TERM_PROGRAM": "iTerm.app"}
    assert detect_backend(env, which=lambda _x: "/usr/bin/it2") is BackendType.TMUX

    # iTerm2 + it2 → iterm2
    env = {"TERM_PROGRAM": "iTerm.app"}
    assert detect_backend(
        env, which=lambda x: "/usr/bin/it2" if x == "it2" else None
    ) is (BackendType.ITERM2)

    # 只有 tmux 可用 → tmux
    env = {}
    assert detect_backend(
        env, which=lambda x: "/usr/bin/tmux" if x == "tmux" else None
    ) is (BackendType.TMUX)

    # 什么都没有 → in-process（Windows 常态）
    assert detect_backend({}, which=lambda _x: None) is BackendType.IN_PROCESS


def test_make_backend():
    assert isinstance(make_backend(BackendType.IN_PROCESS), InProcessBackend)
    assert isinstance(make_backend(BackendType.TMUX), PaneBackend)
    assert make_backend(BackendType.ITERM2).type() is BackendType.ITERM2


def test_in_process_spawn(tmp_path):
    cancelled = []

    async def launcher(req):
        return "task-1", lambda: cancelled.append(req.member)

    backend = InProcessBackend()
    req = _req(tmp_path, launcher=launcher)
    pane_id, agent_id = asyncio.run(backend.spawn(req))

    assert pane_id == ""
    assert agent_id == "task-1"
    assert backend.type() is BackendType.IN_PROCESS
    asyncio.run(backend.wake("", agent_id))  # no-op 不报错
    asyncio.run(backend.kill("", agent_id))
    assert cancelled == ["alice"]


def test_in_process_requires_launcher(tmp_path):
    with pytest.raises(BackendError):
        asyncio.run(InProcessBackend().spawn(_req(tmp_path)))


def test_build_member_command(tmp_path):
    req = _req(tmp_path, model="qwen3", plan_mode_required=True)
    cmd = build_member_command(req)
    joined = " ".join(cmd)
    assert "-m helpcode.team.worker" in joined
    assert "--team t" in joined and "--member alice" in joined
    assert "--worktree" in joined
    assert "--prompt-file" in joined
    assert "--model qwen3" in joined
    assert "--plan-mode" in joined
    assert Path(cmd[cmd.index("--prompt-file") + 1]).name == "task.txt"


def _fake_run(stdout="", code=0, stderr=""):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    return run, calls


def test_pane_spawn_success(tmp_path, monkeypatch):
    run, calls = _fake_run(stdout="%1\n")
    monkeypatch.setattr("helpcode.team.backend.subprocess.run", run)

    backend = PaneBackend(BackendType.TMUX)
    pane_id, agent_id = asyncio.run(backend.spawn(_req(tmp_path)))

    assert pane_id == "%1"
    assert agent_id.startswith("pane-")
    assert calls[0][:2] == ["tmux", "split-window"]
    assert calls[1][:2] == ["tmux", "send-keys"]
    # 任务正文落文件，不进命令行
    assert (tmp_path / "sess" / "task.txt").read_text(encoding="utf-8") == "写点东西"


def test_pane_failure_raises(tmp_path, monkeypatch):
    run, _calls = _fake_run(code=1, stderr="no server running")
    monkeypatch.setattr("helpcode.team.backend.subprocess.run", run)

    backend = PaneBackend(BackendType.TMUX)
    with pytest.raises(BackendError, match="no server running"):
        asyncio.run(backend.spawn(_req(tmp_path)))


def test_pane_kill_and_wake(tmp_path, monkeypatch):
    run, calls = _fake_run(stdout="")
    monkeypatch.setattr("helpcode.team.backend.subprocess.run", run)

    backend = PaneBackend(BackendType.TMUX)
    asyncio.run(backend.wake("%2", "pane-1"))
    asyncio.run(backend.kill("%2", "pane-1"))
    assert calls[0][:2] == ["tmux", "send-keys"]
    assert calls[1][:2] == ["tmux", "kill-pane"]


def test_worker_parse_args():
    args = parse_args(
        [
            "--team",
            "t",
            "--member",
            "alice",
            "--worktree",
            "/tmp/wt",
            "--session-dir",
            "/tmp/sess",
            "--prompt-file",
            "/tmp/task.txt",
            "--plan-mode",
        ]
    )
    assert args.team == "t" and args.member == "alice"
    assert args.plan_mode is True
    assert args.model == ""
