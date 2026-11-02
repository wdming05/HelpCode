"""端到端：CLI 装配、恢复不写标记、两段同时超限。"""

import asyncio
import json
from pathlib import Path

import pytest

from helpcode import cli
from helpcode.config import Config, ProviderConfig
from helpcode.memory import index as index_mod
from helpcode.memory import instructions as ins
from helpcode.memory import session as ses
from helpcode.prompt import build_system_prompt

CFG = Config(
    providers=[ProviderConfig(name="q", protocol="openai", api_key="k", model="m")]
)


def _seed_session(root: Path, sid: str, title: str = "旧问题") -> Path:
    ses.ensure_session_dir(root, sid)
    path = root / ".helpcode" / "sessions" / sid / "conversation.jsonl"
    records = [
        {
            "role": "user",
            "content": title,
            "ts": "2026-10-24T10:00:00+08:00",
            "model": "m",
        },
        {
            "role": "assistant",
            "content": "旧回答",
            "ts": "2026-10-24T10:00:01+08:00",
            "model": "m",
        },
    ]
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    return path


def test_cli_wiring_with_restore(tmp_path, monkeypatch):
    """CLI 把恢复产物传进 App：conv / writer / restore / sessions 都在（T22）。"""
    monkeypatch.chdir(tmp_path)
    sid = "20261024-100000-aaaa"
    _seed_session(tmp_path, sid)

    captured: dict = {}

    class _FakeApp:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            captured["providers"] = args[0] if args else None

        async def run_async(self):
            return None

    monkeypatch.setattr(cli, "HelpCodeApp", _FakeApp)

    asyncio.run(cli._run(CFG))

    assert captured["session_id"] == sid
    assert captured["restore"] is not None
    assert captured["restore"].messages
    assert captured["conv"].messages()[0].content == "旧问题"
    assert captured["sessions"][0].session_id == sid


def test_new_session_when_no_history(tmp_path, monkeypatch):
    """没有历史 → 新建会话，restore 为 None，并真的建出目录。"""
    monkeypatch.chdir(tmp_path)
    captured: dict = {}

    class _FakeApp:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            return None

    monkeypatch.setattr(cli, "HelpCodeApp", _FakeApp)

    asyncio.run(cli._run(CFG))

    assert captured["restore"] is None
    assert captured["session_id"] is not None
    assert ses.jsonl_path(tmp_path, captured["session_id"]).parent.is_dir()


def test_restore_does_not_write_compact_marker(tmp_path):
    """恢复后文件行数不变、没有新增 compact 标记（D7）。

    用 replace_messages 灌历史会立刻写一行标记 + 整份历史——文件白白变长一倍，
    而且下次恢复会把「恢复」当成一次「压缩」。
    """
    sid = "20261024-100000-aaaa"
    path = _seed_session(tmp_path, sid)
    before = path.read_text(encoding="utf-8")
    before_lines = len(before.splitlines())

    latest = ses.scan(tmp_path)[0]
    conv, writer, _, report = cli._build_conversation(tmp_path, latest)
    writer.close()

    after = path.read_text(encoding="utf-8")
    assert len(after.splitlines()) == before_lines
    assert '"type": "compact"' not in after
    assert [m.content for m in conv.messages()] == ["旧问题", "旧回答"]
    assert report.summary_line() == ""


def test_append_after_restore_goes_to_same_file(tmp_path):
    """恢复后再追加 → 写进原文件、无新目录（AC38）。"""
    sid = "20261024-100000-aaaa"
    path = _seed_session(tmp_path, sid)
    latest = ses.scan(tmp_path)[0]

    conv, writer, session_id, _ = cli._build_conversation(tmp_path, latest)
    conv.add_user("接着聊")
    writer.close()

    assert session_id == sid
    assert len(ses.scan(tmp_path)) == 1  # 没有多出目录
    assert "接着聊" in path.read_text(encoding="utf-8")


def test_compaction_writes_marker_then_messages(tmp_path):
    """压缩回调：先写一行标记，再逐条追加（D6）。"""
    sid = "20261024-100000-aaaa"
    path = _seed_session(tmp_path, sid)
    latest = ses.scan(tmp_path)[0]

    conv, writer, _, _ = cli._build_conversation(tmp_path, latest)
    from helpcode.llm import Message

    conv.replace_messages(
        [
            Message(role="user", content="压缩后的唯一一条"),
            Message(role="assistant", content="回答"),
        ]
    )
    writer.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(ln) for ln in lines]
    marker_idx = next(i for i, r in enumerate(records) if r.get("type") == "compact")
    assert records[marker_idx + 1]["content"] == "压缩后的唯一一条"
    assert marker_idx == 2  # 前两行是恢复前就有的

    # 回放只取标记之后 → 与内存一致
    from helpcode.memory.restore import restore

    assert [m.content for m in restore(path).messages] == [
        "压缩后的唯一一条",
        "回答",
    ]


def test_instructions_and_index_both_oversize(tmp_path):
    """两段同时超限：各自的截断标记都在、互不挤占、指令在记忆之前（T23）。"""
    root = tmp_path / "proj"
    home = tmp_path / "home"
    (root / ".helpcode").mkdir(parents=True)
    (home / ".helpcode" / "memory").mkdir(parents=True)
    (root / ".helpcode" / "memory").mkdir(parents=True)

    (root / "AGENTS.md").write_text("规则行\n" * 8000, encoding="utf-8")  # ~40 KB
    (root / ".helpcode" / "memory" / "index.md").write_text(
        "".join(f"- 条目 {i} 内容\n" for i in range(300)), encoding="utf-8"
    )

    instruction_set = ins.load(root, home)
    bundle = index_mod.load(
        home / ".helpcode" / "memory", root / ".helpcode" / "memory"
    )
    system = build_system_prompt(instruction_set.render(), index_mod.render(bundle))

    # ① 两段各自的截断痕迹都在
    assert "已截断" in system
    assert "已裁剪" in system
    # ② 指令在记忆之前
    assert system.index("<!-- 来源: AGENTS.md -->") < system.index("### 项目级记忆")
    # ③ 互不挤占：指令仍保留了接近 32 KB，索引仍有 200 行
    assert instruction_set.layers[0].truncated is True
    assert len(bundle.project.splitlines()) == 200


def test_index_dropped_note_still_on_disk(tmp_path):
    """裁剪只影响注入，不删笔记文件（AC27）。"""
    root = tmp_path / "proj"
    home = tmp_path / "home"
    mem = root / ".helpcode" / "memory"
    mem.mkdir(parents=True)
    (home / ".helpcode" / "memory").mkdir(parents=True)

    note = mem / "long.md"
    note.write_text("---\ntype: user_preference\n---\n正文\n", encoding="utf-8")
    (mem / "index.md").write_text(
        "".join(f"- 条目 {i}\n" for i in range(300)), encoding="utf-8"
    )

    bundle = index_mod.load(home / ".helpcode" / "memory", mem)

    assert bundle.dropped > 0
    assert note.exists()


def test_readonly_sessions_dir_degrades(tmp_path, monkeypatch):
    """sessions 目录不可写 → 启动成功、writer 停用、不抛（AC30）。

    Windows 没有真正的只读目录，所以用 monkeypatch 让 mkdir 抛 OSError 模拟。
    """
    monkeypatch.chdir(tmp_path)

    def boom(self, *args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "mkdir", boom)

    conv, writer, session_id, restored = cli._build_conversation(tmp_path, None)

    assert writer is None
    assert session_id is None
    assert restored is None
    assert conv.messages() == []


@pytest.mark.parametrize("count", [1, 3])
def test_scan_and_cleanup_cooperate(tmp_path, count):
    """清理与扫描的顺序：清理前扫描、keep 保护目标，目标不会消失。"""
    sids = [f"2026102{i}-100000-aaaa" for i in range(count)]
    for sid in sids:
        _seed_session(tmp_path, sid)

    sessions = ses.scan(tmp_path)
    from helpcode.memory import cleanup as cleanup_mod

    cleanup_mod.run(tmp_path, keep=sessions[0].session_id)

    assert ses.jsonl_path(tmp_path, sessions[0].session_id).exists()
