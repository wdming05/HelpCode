"""内置命令的行为测试（用 FakeHost，不依赖 Textual）。"""

import asyncio
from pathlib import Path

from helpcode.command.builtin import build_default_registry
from helpcode.command.host import HostSnapshot, MemoryOverview
from helpcode.command.types import CommandContext
from helpcode.llm import TokenUsage
from helpcode.memory.session import SessionMeta
from helpcode.permission import Mode


class FakeHost:
    def __init__(self, **snap_overrides):
        self.shown: list[str] = []
        self.errors: list[str] = []
        self.sent: list[str] = []
        self.mode = Mode.DEFAULT
        self.compact_result = "已压缩：3 条 → 1 条"
        self.new_sid = "20261024-120000-aaaa"
        self.cleared = False
        self.picker_opened = False
        self.quit_called = False
        snap = {
            "model": "qwen3.7-max",
            "session_id": "20261024-120000-aaaa",
            "message_count": 5,
            "context_window": 200000,
            "context_used": 12000,
            "mode": Mode.DEFAULT,
            "usage": TokenUsage(input_tokens=1000, output_tokens=50, cache_read=500),
            "sessions": (),
            "memory": MemoryOverview(
                layers=(("AGENTS.md", True), (".helpcode/AGENTS.md", False)),
                index_lines=(("用户级", 3), ("项目级", 7)),
                note_count=2,
            ),
        }
        snap.update(snap_overrides)
        self.snapshot_data = HostSnapshot(**snap)

    # ---- 协议实现 ----
    def snapshot(self):
        return self.snapshot_data

    def show(self, text):
        self.shown.append(text)

    def show_error(self, text):
        self.errors.append(text)

    def clear_screen(self):
        self.cleared = True

    async def send_prompt(self, text):
        self.sent.append(text)

    async def compact(self):
        return self.compact_result

    def get_mode(self):
        return self.mode

    def set_mode(self, mode):
        self.mode = mode

    def refresh_status(self):
        pass

    async def start_new_session(self):
        return self.new_sid

    def open_session_picker(self):
        self.picker_opened = True

    async def quit(self):
        self.quit_called = True


def _run(cmd_name, host, args=""):
    registry = build_default_registry()
    cmd = registry.resolve(cmd_name)
    ctx = CommandContext(cmd, args, host, registry)
    return asyncio.run(cmd.handler(ctx))


def _meta(sid="20261020-100000-aaaa", title="旧会话", messages=2, model="m"):
    return SessionMeta(
        session_id=sid,
        dir=Path("."),
        jsonl=Path(".") / "conversation.jsonl",
        title=title,
        messages=messages,
        model=model,
        updated=0.0,
        size_bytes=100,
    )


# ---------------------------------------------------------------- help


def test_help_lists_visible_commands():
    host = FakeHost()
    _run("help", host)
    text = host.shown[0]
    for name in (
        "help",
        "compact",
        "clear",
        "plan",
        "do",
        "session",
        "memory",
        "permission",
        "status",
        "review",
    ):
        assert f"/{name}" in text


def test_help_hides_exit():
    host = FakeHost()
    _run("help", host)
    assert "/exit" not in host.shown[0]
    assert "quit" not in host.shown[0]


def test_help_detail():
    host = FakeHost()
    _run("help", host, args="review")
    text = host.shown[0]
    assert "/review" in text
    assert "审查" in text


def test_help_detail_unknown():
    host = FakeHost()
    _run("help", host, args="nope")
    assert "未找到" in host.shown[0]


# ---------------------------------------------------------------- status


def test_status_fields():
    host = FakeHost()
    _run("status", host)
    text = host.shown[0]
    assert "qwen3.7-max" in text
    assert "20261024-120000-aaaa" in text
    assert "5 条" in text
    assert "200000" in text


def test_status_without_usage():
    host = FakeHost(usage=None)
    _run("status", host)
    assert "尚未请求" in host.shown[0]


# ---------------------------------------------------------------- memory


def test_memory_overview():
    host = FakeHost()
    _run("memory", host)
    text = host.shown[0]
    assert "AGENTS.md" in text
    assert "项目级：7 行" in text
    assert "2 个" in text


# ---------------------------------------------------------------- 模式


def test_plan_sets_mode():
    host = FakeHost()
    _run("plan", host)
    assert host.mode == Mode.PLAN


def test_do_sets_mode_and_sends_prompt():
    host = FakeHost()
    _run("do", host)
    assert host.mode == Mode.DEFAULT
    assert len(host.sent) == 1


def test_permission_show():
    host = FakeHost()
    _run("permission", host)
    assert "DEFAULT" in host.shown[0]


def test_permission_switch():
    host = FakeHost()
    _run("permission", host, args="plan")
    assert host.mode == Mode.PLAN


def test_permission_switch_accept_edits():
    host = FakeHost()
    _run("permission", host, args="accept_edits")
    assert host.mode == Mode.ACCEPT_EDITS


def test_permission_invalid_keeps_mode():
    host = FakeHost()
    host.mode = Mode.PLAN
    _run("permission", host, args="乱写")
    assert host.mode == Mode.PLAN
    assert host.errors


# ---------------------------------------------------------------- 动作


def test_compact_shows_notice():
    host = FakeHost()
    _run("compact", host)
    assert host.shown[0] == "已压缩：3 条 → 1 条"


def test_clear_starts_new_session():
    host = FakeHost()
    _run("clear", host)
    assert host.cleared
    assert "20261024-120000-aaaa" in host.shown[0]


def test_clear_failure_reports():
    host = FakeHost()
    host.new_sid = None
    _run("clear", host)
    assert not host.cleared
    assert host.errors


def test_session_lists_and_opens():
    host = FakeHost(
        sessions=(_meta(), _meta(sid="20261021-100000-bbbb", title="新会话"))
    )
    _run("session", host)
    assert "旧会话" in host.shown[0]
    assert host.picker_opened


def test_session_empty_hint():
    host = FakeHost()
    _run("session", host)
    assert "没有历史会话" in host.shown[0]
    assert not host.picker_opened


# ---------------------------------------------------------------- prompt


def test_review_default_prompt():
    host = FakeHost()
    _run("review", host)
    assert "未提交" in host.sent[0]


def test_review_with_args():
    host = FakeHost()
    _run("review", host, args="src/helpcode/command")
    assert "src/helpcode/command" in host.sent[0]


def test_exit_calls_quit():
    host = FakeHost()
    _run("exit", host)
    assert host.quit_called


def test_all_commands_runnable_with_fake_host():
    registry = build_default_registry()
    for cmd in registry.visible():
        host = FakeHost()
        asyncio.run(cmd.handler(CommandContext(cmd, "", host, registry)))
