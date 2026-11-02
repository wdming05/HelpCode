"""第一层落盘（spill）单测。

阈值用 monkeypatch 缩小到几百字符，避免构造几十 KB 的假数据。
"""

import re
from pathlib import Path

from helpcode.context import spill
from helpcode.conversation import Conversation
from helpcode.llm import Message, ToolCall
from helpcode.tool import ToolResult
from helpcode.tool.read_file import ReadFileTool


def _build(sizes: dict[str, int], tool: str = "bash") -> Conversation:
    """构造「一条 assistant(tool_calls) + N 条 tool 结果」的历史。"""
    conv = Conversation()
    conv.add_user("跑一下")
    conv.add_assistant_tool_calls(
        [ToolCall(id=cid, name=tool, input={}) for cid in sizes]
    )
    for cid, n in sizes.items():
        conv.add_tool_result(cid, ToolResult(ok=True, content="a" * n))
    return conv


def _spilled_files(root: Path) -> list[Path]:
    d = root / spill.SPILL_DIR
    return sorted(d.glob("*.txt")) if d.exists() else []


def _rel_path(content: str) -> str:
    m = re.search(r"文件: (.+)", content)
    assert m, content
    return m.group(1)


def test_single_oversize_spilled(tmp_path, monkeypatch):
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 100)
    monkeypatch.setattr(spill, "BATCH_RESULT_MAX", 10_000)
    conv = _build({"c1": 800})  # 800 字符 ≈ 200 token

    spilled, warns = spill.apply(conv, tmp_path)

    assert spilled == 1
    assert not warns
    msg = conv.messages()[2]
    assert msg.content.startswith(spill.SPILL_MARK)
    assert "前 20 行预览" in msg.content
    files = _spilled_files(tmp_path)
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "a" * 800  # 逐字节无损
    assert len(conv.messages()) == 3  # 条数不变


def test_spill_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 100)
    monkeypatch.setattr(spill, "BATCH_RESULT_MAX", 10_000)
    conv = _build({"c1": 800})

    spill.apply(conv, tmp_path)
    first = conv.messages()[2].content
    spilled, _ = spill.apply(conv, tmp_path)

    assert spilled == 0
    assert len(_spilled_files(tmp_path)) == 1
    assert conv.messages()[2].content == first


def test_batch_spills_largest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 10_000)  # 关掉单条规则
    monkeypatch.setattr(spill, "BATCH_RESULT_MAX", 200)  # 合计上限 200 token
    conv = _build({"c1": 400, "c2": 800, "c3": 400, "c4": 800})  # 100/200/100/200

    spilled, _ = spill.apply(conv, tmp_path)

    assert spilled == 2  # 落盘两条大的后剩 200，刚好不超
    contents = sorted(
        len(f.read_text(encoding="utf-8")) for f in _spilled_files(tmp_path)
    )
    assert contents == [800, 800]  # 落盘的确实是最长的两条


def test_spilled_file_readable_by_read_file(tmp_path, monkeypatch):
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 100)
    conv = _build({"c1": 800})
    spill.apply(conv, tmp_path)

    rel = _rel_path(conv.messages()[2].content)
    result = ReadFileTool().run(path=str(tmp_path / rel))

    assert result.ok
    assert "aaaa" in result.content


def test_tool_results_content_also_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 100)
    conv = _build({"c1": 800})
    spill.apply(conv, tmp_path)

    msg = conv.messages()[2]
    assert msg.tool_results[0].content == msg.content
    assert msg.tool_results[0].ok is True
    assert msg.tool_results[0].is_error is False


def test_oserror_keeps_content_and_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 100)

    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    conv = _build({"c1": 800})

    spilled, warns = spill.apply(conv, tmp_path)

    assert spilled == 0
    assert warns and "disk full" in warns[0]
    assert conv.messages()[2].content == "a" * 800  # 原文未动


def test_user_and_assistant_messages_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 100)
    conv = Conversation()
    conv.add_user("b" * 800)  # 超长 user 消息
    conv.add_assistant("c" * 800)
    conv.add_assistant_tool_calls([ToolCall(id="c1", name="bash", input={})])
    conv.add_tool_result("c1", ToolResult(ok=True, content="a" * 800))

    spilled, _ = spill.apply(conv, tmp_path)

    msgs = conv.messages()
    assert spilled == 1
    assert msgs[0].content == "b" * 800
    assert msgs[1].content == "c" * 800


def test_preview_contains_first_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 10)
    content = "\n".join(f"line{i}" for i in range(50))
    conv = Conversation()
    conv.add_user("x")
    conv.add_assistant_tool_calls([ToolCall(id="c1", name="grep", input={})])
    conv.add_tool_result("c1", ToolResult(ok=True, content=content))

    spill.apply(conv, tmp_path)

    msg_content = conv.messages()[2].content
    assert "line0" in msg_content
    assert "line19" in msg_content
    assert "line20" not in msg_content  # 只留前 20 行
    assert "不要凭预览推断未看到的部分" in msg_content


def test_spill_filename_does_not_overwrite(tmp_path, monkeypatch):
    """同一秒 + 同一 tool_call_id：后写的不能覆盖先写的（期望 .txt / -2.txt）。"""
    from datetime import datetime as real_dt

    class _FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 23, 12, 0, 0)

    monkeypatch.setattr(spill, "datetime", _FixedDT)
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 10)
    monkeypatch.setattr(spill, "BATCH_RESULT_MAX", 10_000)

    for _ in range(2):
        spill.apply(_build({"c1": 200}), tmp_path)

    files = _spilled_files(tmp_path)
    assert len(files) == 2
    assert sorted(f.stem[-2:] for f in files) == ["-2", "c1"]
    # 两个文件内容都在，没有被覆盖成一份
    assert {f.read_text(encoding="utf-8") for f in files} == {"a" * 200}


def test_preview_is_deterministic(tmp_path, monkeypatch):
    """替换串必须确定性：同一 content 两次构造逐字节相同（prompt cache 稳定）。"""
    from helpcode.llm import Message

    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 0)
    msg_a = Message(role="tool", content="x\n" * 100, tool_call_id="cid")
    msg_b = Message(role="tool", content="x\n" * 100, tool_call_id="cid")

    a = spill._spill_one(msg_a, "bash", tmp_path, [])
    b = spill._spill_one(msg_b, "bash", tmp_path, [])
    # 文件名带序号（第二次 -2），把路径段抹掉后比较
    strip = lambda s: re.sub(r"文件: .+", "文件: <path>", s)
    assert strip(a) == strip(b)
    assert "原长 100 行" in a


def test_real_threshold_50k_chars_spills_and_reads_back(tmp_path):
    """不 monkeypatch 阈值：5 万字符的真实量级也能触发落盘并可回读（场景 9 组件层）。"""

    big = "\n".join(f"row {i}: " + "z" * 80 for i in range(600))  # 约 5 万字符
    assert len(big) > 50_000

    call_id = "call-big"
    conv = Conversation()
    conv.add_user("跑个大命令")
    conv.add_assistant_tool_calls([ToolCall(id=call_id, name="bash", input={})])
    conv.add_tool_result(call_id, ToolResult(ok=True, content=big))

    spilled, warns = spill.apply(conv, tmp_path)

    assert spilled == 1 and not warns
    msg = conv.messages()[2]
    assert msg.content.startswith(spill.SPILL_MARK)
    assert len(msg.content) < 3000  # 预览远小于原文

    rel = _rel_path(msg.content)
    files = _spilled_files(tmp_path)
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == big  # 逐字节无损

    back = ReadFileTool().run(path=str(tmp_path / rel))
    assert back.ok
    assert "row 0" in back.content


# ---------------------------------------------------------------- 会话子目录（ch09）


def test_spill_into_session_subdir(tmp_path, monkeypatch):
    """落盘进会话目录下的 tool-results/（AC39/F12）。"""
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 10)
    subdir = Path(".helpcode") / "sessions" / "20261024-120000-ab12" / "tool-results"

    conv = _build({"c1": 400})
    spill.apply(conv, tmp_path, subdir)

    files = list((tmp_path / subdir).glob("*.txt"))
    assert len(files) == 1
    assert not (tmp_path / ".helpcode" / "tool-results").exists()


def test_spill_path_still_relative_to_root(tmp_path, monkeypatch):
    """消息里的路径必须相对**项目根**，否则 read_file 找不到（AC39）。

    这是本章最容易写错的一行：写成 `tool-results/x.txt`（相对会话目录）时，
    模型拿这个路径去 read_file 会失败，而落盘本身完全正常。
    """
    from helpcode.tool.read_file import ReadFileTool

    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 10)
    subdir = Path(".helpcode") / "sessions" / "20261024-120000-ab12" / "tool-results"

    conv = _build({"c1": 400})
    spill.apply(conv, tmp_path, subdir)

    content = conv.messages()[2].content
    assert ".helpcode/sessions/20261024-120000-ab12/tool-results/" in content

    rel = _rel_path(content)
    monkeypatch.chdir(tmp_path)  # 模拟「TUI 在项目根启动」
    back = ReadFileTool().run(path=rel)
    assert back.ok
    assert back.content.strip()


def test_default_subdir_unchanged(tmp_path, monkeypatch):
    """不传 subdir → 行为与 ch08 完全一致（回归）。"""
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 10)

    conv = _build({"c1": 400})
    spill.apply(conv, tmp_path)

    assert list((tmp_path / ".helpcode" / "tool-results").glob("*.txt"))


# ---------------------------------------------------------------- 豁免与预览截断


def _build_mixed(spec: list[tuple[str, str, int]]) -> Conversation:
    """构造混合工具的历史：[(call_id, 工具名, 内容长度)]。"""
    conv = Conversation()
    conv.add_user("跑一下")
    conv.add_assistant_tool_calls(
        [ToolCall(id=cid, name=name, input={}) for cid, name, _ in spec]
    )
    for cid, _, n in spec:
        conv.add_tool_result(cid, ToolResult(ok=True, content="a" * n))
    return conv


def test_read_file_result_never_spilled(tmp_path, monkeypatch):
    """read_file 的结果不落盘 —— 否则模型读回落盘文件会再次超阈值，形成死循环。"""
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 100)
    conv = _build_mixed([("c1", "read_file", 5000)])

    spilled, warns = spill.apply(conv, tmp_path)

    assert spilled == 0
    assert not warns
    assert not _spilled_files(tmp_path)
    assert conv.messages()[2].content == "a" * 5000


def test_exempt_does_not_disable_other_tools(tmp_path, monkeypatch):
    """豁免只针对 read_file：同一批里的 bash 结果照常落盘。"""
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 100)
    conv = _build_mixed([("c1", "read_file", 5000), ("c2", "bash", 5000)])

    spilled, _ = spill.apply(conv, tmp_path)

    assert spilled == 1
    assert conv.messages()[2].content == "a" * 5000
    assert conv.messages()[3].content.startswith(spill.SPILL_MARK)


def test_preview_truncated_by_chars_on_single_long_line(tmp_path, monkeypatch):
    """单行超长（minified JSON）时预览必须按字符截 —— 只按行截会让预览等于全文。"""
    monkeypatch.setattr(spill, "TOOL_RESULT_MAX", 100)
    msg = Message(role="tool", content="x" * 30000, tool_call_id="c1")

    preview = spill._spill_one(msg, "bash", tmp_path, [])

    assert preview is not None
    assert len(preview) < 4000  # 不随原文线性增长
    assert "预览已截断" in preview
    assert "原长 1 行" in preview
