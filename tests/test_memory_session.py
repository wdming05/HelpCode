"""会话 ID、目录布局、ID 时间解析。"""

import re
from datetime import datetime, timedelta

import pytest

from helpcode.memory import session as ses
from helpcode.memory.constants import JSONL_NAME, TOOL_RESULTS_NAME


def test_session_id_format():
    """`YYYYMMDD-HHMMSS-xxxx`（AC9）。"""
    sid = ses.new_session_id()
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", sid)


def test_same_second_unique():
    """同一秒内连建 50 个也不撞名（AC9）——不靠 sleep 跨秒。"""
    fixed = datetime(2026, 10, 24, 12, 0, 0)
    ids = {ses.new_session_id(fixed) for _ in range(50)}
    assert len(ids) == 50
    assert all(sid.startswith("20261024-120000-") for sid in ids)


def test_parse_session_time():
    """ID 前 15 位解析出创建时刻。(D23)"""
    assert ses.parse_session_time("20261024-120000-ab12") == datetime(
        2026, 10, 24, 12, 0, 0
    )


def test_parse_session_time_ignores_suffix():
    """后缀不参与解析：换成非十六进制也照样成功。(D23)"""
    assert ses.parse_session_time("20261024-120000-zzzz") == datetime(
        2026, 10, 24, 12, 0, 0
    )


@pytest.mark.parametrize("bad", ["legacy", "", "2026-10-24", "20261024"])
def test_parse_session_time_invalid_raises(bad):
    """非法 ID 必须抛 ValueError，不能返回 None。(D23)"""
    with pytest.raises(ValueError):
        ses.parse_session_time(bad)


def test_dir_layout(tmp_path):
    """目录形状：sessions/<id>/{conversation.jsonl, tool-results/}（AC9/F12）。"""
    sid = ses.new_session_id()
    directory = ses.ensure_session_dir(tmp_path, sid)

    assert directory == tmp_path / ".helpcode" / "sessions" / sid
    assert ses.jsonl_path(tmp_path, sid) == directory / JSONL_NAME
    assert (directory / TOOL_RESULTS_NAME).is_dir()


def test_tool_results_dir_with_and_without_session(tmp_path):
    """有会话用会话内目录；没有则退回全局目录（F12）。"""
    with_id = ses.tool_results_dir(tmp_path, "20261024-120000-ab12")
    assert with_id == (
        tmp_path / ".helpcode" / "sessions" / "20261024-120000-ab12" / "tool-results"
    )

    without = ses.tool_results_dir(tmp_path, None)
    assert without == tmp_path / ".helpcode" / "tool-results"


def test_ensure_dir_degrades_on_oserror(tmp_path, monkeypatch):
    """建目录失败时抛 OSError（由调用方决定降级），而不是静默返回。"""
    from pathlib import Path

    def boom(self, *args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "mkdir", boom)
    with pytest.raises(OSError):
        ses.ensure_session_dir(tmp_path, "20261024-120000-ab12")


def test_ensure_dir_is_idempotent(tmp_path):
    """重复建同一会话目录不报错（恢复后写回同一目录要用）。"""
    sid = "20261024-120000-ab12"
    first = ses.ensure_session_dir(tmp_path, sid)
    second = ses.ensure_session_dir(tmp_path, sid)
    assert first == second


def test_parse_session_time_roundtrip_with_id(tmp_path):
    """ID → 时间 → 与生成时刻一致（±1 秒内，防跨秒边界）。"""
    now = datetime.now().replace(microsecond=0)
    sid = ses.new_session_id(now)
    assert ses.parse_session_time(sid) == now


def test_ttl_arithmetic():
    """30 天判据基于 ID 时间做算术，不依赖文件系统。"""
    created = ses.parse_session_time("20260924-120000-ab12")
    now = created + timedelta(days=31)
    assert (now - created).days == 31


# ---------------------------------------------------------------- Writer


def _writer(tmp_path, model="qwen3.7-max"):
    sid = "20261024-120000-ab12"
    ses.ensure_session_dir(tmp_path, sid)
    return ses.SessionWriter(ses.jsonl_path(tmp_path, sid), model), ses.jsonl_path(
        tmp_path, sid
    )


def _read_jsonl(path):
    import json

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_append_one_line_per_message(tmp_path):
    """一条消息一行，每行都是合法 JSON（AC10）。"""
    from helpcode.llm import Message

    w, path = _writer(tmp_path)
    w.append(Message(role="user", content="你好"))
    w.append(Message(role="assistant", content="在的"))
    w.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert _read_jsonl(path)[0]["content"] == "你好"


def test_record_has_all_fields(tmp_path):
    """role / content / ts / model 必有；tool_calls / tool_call_id 按角色出现（AC41）。"""
    from helpcode.llm import Message, ToolCall

    w, path = _writer(tmp_path)
    w.append(Message(role="user", content="跑个命令"))
    w.append(
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="bash", input={"command": "ls"})],
        )
    )
    w.append(Message(role="tool", content="文件列表", tool_call_id="c1"))
    w.close()

    records = _read_jsonl(path)
    assert all(r["role"] and "ts" in r and r["model"] == "qwen3.7-max" for r in records)
    assert records[1]["tool_calls"][0] == {
        "id": "c1",
        "name": "bash",
        "input": {"command": "ls"},
    }
    assert records[2]["tool_call_id"] == "c1"


def test_fsync_called_per_append(tmp_path, monkeypatch):
    """每条都 flush + fsync，且顺序是 flush 在前（AC36）。"""
    from helpcode.llm import Message
    from helpcode.memory import session as session_mod

    events: list[str] = []

    monkeypatch.setattr(session_mod, "_flush", lambda fh: events.append("flush"))
    monkeypatch.setattr(session_mod, "_fsync", lambda fh: events.append("fsync"))

    w, _ = _writer(tmp_path)
    w.append(Message(role="user", content="a"))
    w.append(Message(role="assistant", content="b"))
    w.close()

    assert events == ["flush", "fsync", "flush", "fsync"]


def test_compact_marker_is_bare(tmp_path):
    """标记行只有 type + ts，不含正文，整行 < 200 字节（AC12/D6）。"""
    w, path = _writer(tmp_path)
    w.write_compact_marker()
    w.close()

    line = path.read_text(encoding="utf-8").strip()
    record = _read_jsonl(path)[0]
    assert set(record) == {"type", "ts"}
    assert record["type"] == "compact"
    assert len(line.encode("utf-8")) < 200


def test_compact_marker_before_messages(tmp_path):
    """标记先落盘、消息后追加——顺序反了回放会丢掉标记前的新消息（D6）。"""
    from helpcode.llm import Message

    w, path = _writer(tmp_path)
    w.write_compact_marker()
    w.append_all([Message(role="user", content="压缩后的新历史")])
    w.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert _read_jsonl_lines(lines)[0]["type"] == "compact"
    assert _read_jsonl_lines(lines)[1]["content"] == "压缩后的新历史"


def _read_jsonl_lines(lines):
    import json

    return [json.loads(line) for line in lines if line.strip()]


def test_append_all_filters_time_gap(tmp_path):
    """时间跨度提醒不写进存档（AC17/D8）。"""
    from helpcode.llm import Message
    from helpcode.memory.constants import TIME_GAP_MARK

    w, path = _writer(tmp_path)
    w.append_all(
        [
            Message(role="user", content="旧消息"),
            Message(role="user", content=f"{TIME_GAP_MARK}上次对话在 3 天前"),
        ]
    )
    w.close()

    text = path.read_text(encoding="utf-8")
    assert "旧消息" in text
    assert TIME_GAP_MARK not in text


def test_write_failure_disables_silently(tmp_path, monkeypatch):
    """写失败 → 不抛、disabled 为真、之后不再尝试（N5/AC30）。"""
    from helpcode.llm import Message
    from helpcode.memory import session as session_mod

    w, _path = _writer(tmp_path)
    calls = {"n": 0}

    def boom(fh):
        calls["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(session_mod, "_flush", boom)

    w.append(Message(role="user", content="第一条"))  # 不抛
    assert w.disabled is True
    w.append(Message(role="assistant", content="第二条"))
    assert calls["n"] == 1  # 停用之后没有第二次尝试


def test_after_disabled_no_more_writes(tmp_path, monkeypatch):
    """停用后文件内容不再增长。"""
    from helpcode.llm import Message

    w, path = _writer(tmp_path)
    w.append(Message(role="user", content="好"))
    before = path.stat().st_size

    w._disabled = True
    w.append(Message(role="assistant", content="不该写入"))
    w.close()

    assert path.stat().st_size == before


def test_append_does_not_rewrite_existing_lines(tmp_path):
    """既有行逐字节不变：只追加（AC10）。"""
    from helpcode.llm import Message

    w, path = _writer(tmp_path)
    w.append(Message(role="user", content="第一轮"))
    first = path.read_text(encoding="utf-8")

    w.append(Message(role="assistant", content="第二轮"))
    second = path.read_text(encoding="utf-8")

    assert second.startswith(first)
    assert len(second) > len(first)


# ---------------------------------------------------------------- 扫描


def _make_session(root, sid, records=None, model="qwen3.7-max"):
    """造一个会话目录并写入若干记录。"""
    import json

    ses.ensure_session_dir(root, sid)
    path = ses.jsonl_path(root, sid)
    lines = records or [
        {"role": "user", "content": "第一个问题", "ts": "x", "model": model},
        {"role": "assistant", "content": "第一个回答", "ts": "x", "model": model},
    ]
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines),
        encoding="utf-8",
    )
    return path


def test_scan_title_from_first_user(tmp_path):
    """标题取首条非空 user 消息，压平换行、截到 60 字符（AC11）。"""
    long = "第一行\n第二行 " + "x" * 100
    _make_session(
        tmp_path,
        "20261024-120000-aaaa",
        [
            {"role": "assistant", "content": "我先说", "ts": "x", "model": "m"},
            {"role": "user", "content": long, "ts": "x", "model": "m"},
        ],
    )

    metas = ses.scan(tmp_path)
    assert len(metas) == 1
    assert "\n" not in metas[0].title
    assert len(metas[0].title) <= 60
    assert metas[0].title.startswith("第一行 第二行")


def test_scan_model_from_last_line(tmp_path):
    """模型取最后一个带 model 的行（中途换模型后要显示新的，AC41）。"""
    _make_session(
        tmp_path,
        "20261024-120000-aaaa",
        [
            {"role": "user", "content": "q", "ts": "x", "model": "old-model"},
            {"role": "assistant", "content": "a", "ts": "x", "model": "new-model"},
        ],
    )
    assert ses.scan(tmp_path)[0].model == "new-model"


def test_scan_skips_bad_lines(tmp_path):
    """坏行不影响统计：跳过后条数仍是有效行数（AC13 的扫描侧）。"""
    import json

    path = _make_session(tmp_path, "20261024-120000-aaaa")
    path.write_text(
        path.read_text(encoding="utf-8") + "{ 这行不是 JSON }\n", encoding="utf-8"
    )

    meta = ses.scan(tmp_path)[0]
    assert meta.messages == 2
    assert (
        json.loads(path.read_text(encoding="utf-8").splitlines()[0])["role"] == "user"
    )


def test_scan_sorted_by_id_time(tmp_path):
    """按 ID 时间降序；把旧会话的 mtime 改成未来也不影响（D25 反证）。"""
    import os
    import time

    _make_session(tmp_path, "20261001-100000-aaaa")
    _make_session(tmp_path, "20261020-100000-bbbb")
    _make_session(tmp_path, "20261010-100000-cccc")

    # 让最早的会话 mtime 变成「现在」（甚至未来）
    future = time.time() + 86400
    os.utime(ses.jsonl_path(tmp_path, "20261001-100000-aaaa"), (future, future))

    ids = [m.session_id for m in ses.scan(tmp_path)]
    assert ids == [
        "20261020-100000-bbbb",
        "20261010-100000-cccc",
        "20261001-100000-aaaa",
    ]


def test_scan_ignores_unparsable_id(tmp_path):
    """旧格式 / 手放目录一律不出现（D24）。"""
    import shutil

    good = _make_session(tmp_path, "20261024-120000-aaaa")
    legacy = tmp_path / ".helpcode" / "sessions" / "legacy-session"
    legacy.mkdir(parents=True)
    shutil.copy(good, legacy / "conversation.jsonl")

    ids = [m.session_id for m in ses.scan(tmp_path)]
    assert ids == ["20261024-120000-aaaa"]


def test_scan_ignores_foreign_files(tmp_path):
    """会话目录里的其它文件不算会话，也不影响计数。"""
    _make_session(tmp_path, "20261024-120000-aaaa")
    (
        tmp_path / ".helpcode" / "sessions" / "20261024-120000-aaaa" / "notes.txt"
    ).write_text("x", encoding="utf-8")

    meta = ses.scan(tmp_path)[0]
    assert meta.messages == 2


def test_scan_counts_only_after_marker(tmp_path):
    """压缩标记之前的消息不计入条数（AC12 的扫描侧）。"""
    _make_session(
        tmp_path,
        "20261024-120000-aaaa",
        [
            {"role": "user", "content": "压缩前的旧问题", "ts": "x", "model": "m"},
            {"role": "assistant", "content": "旧回答", "ts": "x", "model": "m"},
            {"type": "compact", "ts": "x"},
            {"role": "user", "content": "压缩后的新问题", "ts": "x", "model": "m"},
        ],
    )

    meta = ses.scan(tmp_path)[0]
    assert meta.messages == 1
    assert meta.title == "压缩后的新问题"


def test_scan_empty_root(tmp_path):
    """没有 sessions 目录 → 空列表，不报错、不创建目录。"""
    assert ses.scan(tmp_path) == []
    assert not (tmp_path / ".helpcode").exists()


def test_scan_size_and_updated(tmp_path):
    """size 取文件大小，updated 取 ID 时间（不是 mtime）。"""
    path = _make_session(tmp_path, "20261024-120000-aaaa")
    meta = ses.scan(tmp_path)[0]

    assert meta.size_bytes == path.stat().st_size
    assert meta.updated == ses.parse_session_time("20261024-120000-aaaa").timestamp()


def test_set_model_fills_later_lines(tmp_path):
    """空 model 建的 writer，补 model 之后每一行都带上（AC41）。

    新建会话时 provider 还没选，writer 只能先传空串。若一直不补，
    整个会话的 jsonl 都没有 model 字段，/resume 只能显示「未知」。
    """
    from helpcode.llm import Message

    w, path = _writer(tmp_path, model="")
    w.append(Message(role="user", content="第一句"))
    w.set_model("qwen3.7-max")
    w.append(Message(role="assistant", content="第二句"))
    w.close()

    records = _read_jsonl(path)
    assert "model" not in records[0]  # provider 未定之前确实没有
    assert records[1]["model"] == "qwen3.7-max"


def test_no_meta_files_in_session_dir(tmp_path):
    """会话目录里只有 conversation.jsonl 与 tool-results/（AC11）。"""
    sid = ses.new_session_id()
    directory = ses.ensure_session_dir(tmp_path, sid)
    w = ses.SessionWriter(ses.jsonl_path(tmp_path, sid), "m")
    w.close()

    names = {p.name for p in directory.iterdir()}
    assert names == {ses.JSONL_NAME, ses.TOOL_RESULTS_NAME}


def test_scan_only_current_project(tmp_path):
    """scan 只看传入的 root：另一个项目里更新的会话不出现（AC18）。"""
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()
    _make_session(a, "20261024-120000-aaaa")
    _make_session(b, "20261025-120000-bbbb")  # 更晚的会话

    ids = [m.session_id for m in ses.scan(a)]
    assert ids == ["20261024-120000-aaaa"]
