"""会话回放：坏行、孤儿、未完成组、时间跨度。"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from helpcode.memory import restore as rs
from helpcode.memory.constants import TIME_GAP_HOURS, TIME_GAP_MARK


def _write(tmp_path: Path, records: list[dict | str]) -> Path:
    """把记录（或原始坏行）写进一个 jsonl。"""
    p = tmp_path / "conversation.jsonl"
    lines = [
        rec if isinstance(rec, str) else json.dumps(rec, ensure_ascii=False)
        for rec in records
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _u(text: str, ts: str = "2026-10-24T12:00:00+08:00") -> dict:
    return {"role": "user", "content": text, "ts": ts, "model": "m"}


def _a(text: str = "", calls: list[dict] | None = None, ts="2026-10-24T12:00:01+08:00"):
    rec = {"role": "assistant", "content": text, "ts": ts, "model": "m"}
    if calls:
        rec["tool_calls"] = calls
    return rec


def _t(call_id: str, text: str = "结果", ts="2026-10-24T12:00:02+08:00") -> dict:
    return {
        "role": "tool",
        "content": text,
        "tool_call_id": call_id,
        "ts": ts,
        "model": "m",
    }


def _call(cid: str, name: str = "bash") -> dict:
    return {"id": cid, "name": name, "input": {"command": "ls"}}


def test_bad_line_skipped(tmp_path):
    """坏行跳过并计数，其它行照常回放（AC13）。"""
    path = _write(tmp_path, [_u("你好"), "{ 这是坏行", _a("回答")])

    report = rs.restore(path)

    assert report.skipped_lines == 1
    assert [m.content for m in report.messages] == ["你好", "回答"]
    assert "跳过 1 行" in report.summary_line()


def test_compact_marker_takes_last(tmp_path):
    """两条标记 → 只回放最后一个之后（AC12）。"""
    path = _write(
        tmp_path,
        [
            _u("最旧"),
            {"type": "compact", "ts": "2026-10-24T12:00:00+08:00"},
            _u("第一次压缩后"),
            {"type": "compact", "ts": "2026-10-24T12:00:00+08:00"},
            _u("第二次压缩后"),
        ],
    )

    report = rs.restore(path)

    assert [m.content for m in report.messages] == ["第二次压缩后"]


def test_lines_before_marker_ignored(tmp_path):
    """标记前的消息一条都不出现——这是标记语义的核心断言（AC12）。"""
    path = _write(
        tmp_path,
        [
            _u("压缩前的问题"),
            _a("压缩前的回答"),
            {"type": "compact", "ts": "x"},
            _u("压缩后的问题"),
        ],
    )

    contents = [m.content for m in rs.restore(path).messages]

    assert contents == ["压缩后的问题"]
    assert "压缩前的" not in "".join(contents)


def test_orphan_tool_dropped(tmp_path):
    """孤儿 tool（无对应调用）被丢弃，历史仍合法（AC14）。"""
    path = _write(tmp_path, [_u("问"), _t("不存在的调用"), _a("答")])

    report = rs.restore(path)

    assert [m.role for m in report.messages] == ["user", "assistant"]
    assert report.skipped_lines == 1


def test_first_message_is_not_tool(tmp_path):
    """以 tool 开头（前面的 assistant 被跳过）→ 丢弃该 tool（AC14）。"""
    path = _write(tmp_path, ["{坏行", _t("c1"), _u("后来")])

    report = rs.restore(path)

    assert report.messages[0].role == "user"


def test_incomplete_group_filled(tmp_path):
    """2 个调用只有 1 条结果 → 已收到的保留，缺的补一条中断结果（AC15）。"""
    path = _write(
        tmp_path,
        [
            _u("跑两个命令"),
            _a(calls=[_call("c1"), _call("c2")]),
            _t("c1"),
            _u("换个方式"),
        ],
    )

    report = rs.restore(path)

    assert report.interrupted_groups == 1
    assert report.filled_results == 1
    assert [m.role for m in report.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    # 已收到的结果原样保留，不能被连坐删掉
    assert report.messages[2].tool_call_id == "c1"
    assert report.messages[2].content == "结果"
    # 缺的那条补成中断说明
    assert report.messages[3].tool_call_id == "c2"
    assert "中断" in report.messages[3].content


def test_interrupted_result_admits_uncertainty(tmp_path):
    """补位措辞必须留在「不确定」：既不说没跑过，也不说跑完了（AC15）。"""
    path = _write(
        tmp_path, [_u("跑命令"), _a(calls=[_call("c1"), _call("c2")]), _t("c1")]
    )

    report = rs.restore(path)
    filled = report.messages[-1]

    # 必须把核实动作交回给模型
    assert "需要自行确认" in filled.content
    assert "可能已经部分生效" in filled.content
    # 不能把话说死——工具可能压根没启动，也可能改了一半
    for claim in ("未执行", "没有执行", "执行失败", "已回滚", "假设成功"):
        assert claim not in filled.content


def test_trailing_pending_filled(tmp_path):
    """文件末尾仍未收齐 → 补一条中断结果（最常见的崩溃现场）。"""
    path = _write(tmp_path, [_u("跑命令"), _a(calls=[_call("c1")])])

    report = rs.restore(path)

    assert report.interrupted_groups == 1
    assert [m.role for m in report.messages] == ["user", "assistant", "tool"]
    assert report.messages[-1].tool_call_id == "c1"


def test_complete_group_kept(tmp_path):
    """完整的一问一调一果保留，tool_call_id 配对正确。"""
    path = _write(tmp_path, [_u("跑"), _a(calls=[_call("c1")]), _t("c1", "结果A")])

    msgs = rs.restore(path).messages

    assert [m.role for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1].tool_calls[0].id == "c1"
    assert msgs[1].tool_calls[0].input == {"command": "ls"}
    assert msgs[2].tool_call_id == "c1"


def test_empty_file(tmp_path):
    """空文件 → 空历史、无告警。"""
    path = tmp_path / "conversation.jsonl"
    path.write_text("", encoding="utf-8")

    report = rs.restore(path)

    assert report.messages == []
    assert report.summary_line() == ""


def test_missing_file_returns_empty(tmp_path):
    """文件不存在（新会话）→ 空历史，不抛。"""
    assert rs.restore(tmp_path / "nope.jsonl").messages == []


def test_summary_line_empty_when_clean(tmp_path):
    """没发生修复 → summary_line() 是空串，UI 不该多写一行。"""
    path = _write(tmp_path, [_u("正常"), _a("正常")])
    assert rs.restore(path).summary_line() == ""


def test_last_ts_recorded(tmp_path):
    """last_ts 取最后一条有效记录的时间。"""
    path = _write(
        tmp_path,
        [_u("a", "2026-10-24T10:00:00+08:00"), _a("b", ts="2026-10-24T11:30:00+08:00")],
    )

    report = rs.restore(path)

    assert report.last_ts == datetime.fromisoformat("2026-10-24T11:30:00+08:00")


def test_time_gap_under_threshold_none():
    """6 小时内不提醒（AC17）。"""
    now = datetime(2026, 10, 24, 12, 0)
    assert rs.time_gap_notice(now - timedelta(hours=6), now) is None


def test_time_gap_over_threshold():
    """超过 24 小时 → 提醒，且带相对时间与「重新读取」的提示（AC17）。"""
    now = datetime(2026, 10, 24, 12, 0)
    msg = rs.time_gap_notice(now - timedelta(days=3), now)

    assert msg is not None
    assert msg.content.startswith(TIME_GAP_MARK)
    assert "3 天" in msg.content
    assert "重新读取" in msg.content


def test_time_gap_boundary():
    """边界：**满** 24 小时就提醒（`>=` 而非 `>`）。

    取 >= 而不是 > 的理由：差一秒的 24 小时和满 24 小时，对「代码可能变了」
    这件事没有区别；用 >= 也避免「刚好一天没说话却没提醒」这种说不清的情况。
    """
    now = datetime(2026, 10, 24, 12, 0)
    msg = rs.time_gap_notice(now - timedelta(hours=TIME_GAP_HOURS), now)
    assert msg is not None

    just_below = now - timedelta(hours=TIME_GAP_HOURS) + timedelta(seconds=1)
    assert rs.time_gap_notice(just_below, now) is None


def test_time_gap_mixed_timezone_no_crash():
    """一边带时区一边不带 → 归一后比较，不抛 TypeError。"""
    now = datetime(2026, 10, 24, 12, 0, tzinfo=None)
    last = datetime(2026, 10, 20, 12, 0).astimezone()

    msg = rs.time_gap_notice(last, now)
    assert msg is not None
