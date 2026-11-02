"""笔记提炼：请求构造、输出解析、写入、索引维护、并发。"""

import json
from datetime import datetime
from pathlib import Path

from helpcode.llm import Message, ToolCall
from helpcode.memory import notes
from helpcode.memory.constants import NOTE_CHUNK_MSGS


def _chunk(n: int = 3) -> list[Message]:
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(Message(role=role, content=f"第{i}条"))
    return out


# ---------------------------------------------------------------- 请求


def test_request_has_no_tools():
    """笔记请求必须 tools=None：它不该触发任何工具调用（AC24）。"""
    req = notes.build_request(_chunk())
    assert req.tools is None
    assert req.system.environment == ""
    assert req.reminder == ""


def test_request_uses_note_system():
    req = notes.build_request(_chunk(), existing_index="- 旧笔记")
    assert "维护一个编程助手助手的长期记忆" in req.system.stable
    assert "- 旧笔记" in req.system.stable
    assert "（暂无）" not in req.system.stable


def test_request_empty_index_placeholder():
    req = notes.build_request(_chunk(), existing_index="   ")
    assert "（暂无）" in req.system.stable


def test_chunk_takes_only_recent_messages():
    """只带最近 NOTE_CHUNK_MSGS 条：笔记请求不该把整段历史都塞进去。"""
    chunk = _chunk(20)
    req = notes.build_request(chunk)
    body = req.messages[0].content

    assert "第19条" in body
    assert "第0条" not in body
    assert len(chunk) > NOTE_CHUNK_MSGS


def test_chunk_records_tool_names():
    """有工具调用时把工具名带上——「用了什么工具」本身是偏好线索。"""
    chunk = [
        Message(role="user", content="跑测试"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="bash", input={"command": "pytest"})],
        ),
    ]
    body = notes.build_request(chunk).messages[0].content
    assert "bash" in body


# ---------------------------------------------------------------- 解析


def _payload(*items) -> str:
    return json.dumps(list(items), ensure_ascii=False)


GOOD = {
    "action": "add",
    "type": "user_preference",
    "target": "user",
    "title": "提交前跑 ruff",
    "description": "用户要求每次提交前格式化",
    "body": "提交前必须跑 ruff format 和 ruff check。",
}


def test_parse_valid():
    updates = notes.parse_updates(_payload(GOOD))

    assert len(updates) == 1
    assert updates[0].title == "提交前跑 ruff"
    assert updates[0].type == "user_preference"
    assert updates[0].target == "user"


def test_parse_fenced_output():
    """模型常加代码围栏和解释文字：要能从中抓出数组。"""
    raw = "好的，我提炼到一条：\n```json\n" + _payload(GOOD) + "\n```\n以上。"
    assert len(notes.parse_updates(raw)) == 1


def test_parse_invalid_returns_empty():
    """解析不出来 → 空列表（这一轮没记东西是正常结果，不是错误）。"""
    for raw in ["", "不记得有什么", "{不是数组}", "[{坏JSON}]"]:
        assert notes.parse_updates(raw) == []


def test_parse_filters_unknown_type():
    """类型不在四类里 → 丢掉该项，宁可不记也不塞错分类。"""
    bad = dict(GOOD, type="random_type")
    assert notes.parse_updates(_payload(bad, GOOD))[0].type == "user_preference"
    assert len(notes.parse_updates(_payload(bad))) == 0


def test_parse_skips_skip_action():
    assert notes.parse_updates(_payload(dict(GOOD, action="skip"))) == []


def test_parse_requires_title():
    assert notes.parse_updates(_payload(dict(GOOD, title="  "))) == []


def test_parse_default_target_is_project():
    """target 缺失或非法时默认 project（更保守：不会污染用户级全局记忆）。"""
    item = dict(GOOD)
    item.pop("target")
    assert notes.parse_updates(_payload(item))[0].target == "project"

    assert (
        notes.parse_updates(_payload(dict(GOOD, target="weird")))[0].target == "project"
    )


def test_parse_keeps_multiple():
    """同一次返回多条时全部保留，顺序不变。"""
    second = dict(GOOD, title="第二条", type="project_knowledge")
    updates = notes.parse_updates(_payload(GOOD, second))
    assert [u.title for u in updates] == ["提交前跑 ruff", "第二条"]


# ---------------------------------------------------------------- 写入


FIXED = datetime(2026, 10, 24, 12, 0, 0)


def _stores(tmp_path):
    home = tmp_path / "home_mem"
    proj = tmp_path / "proj_mem"
    return home, proj


def _update(**kw):
    base = {
        "action": "add",
        "type": "user_preference",
        "target": "project",
        "title": "提交前跑 ruff",
        "description": "说明",
        "body": "正文",
        "file": "",
    }
    base.update(kw)
    return notes.NoteUpdate(**base)


def test_slug_deterministic(tmp_path):
    """同一标题两次生成同一个 slug，不产生 -2 副本（AC28）。"""
    assert notes.slugify("提交前跑 Riff") == notes.slugify("提交前跑 Riff")
    assert notes.slugify("提交前跑 ruff") == notes.slugify("  提交前跑 RUFF  ")


def test_slug_strips_path_separators():
    """标题含 / \\ : → 文件名不含路径分隔符（AC28/N4）。"""
    for title in ["a/b/c", "a\\b", "C: 盘", "../../etc/passwd"]:
        slug = notes.slugify(title)
        assert "/" not in slug
        assert "\\" not in slug
        assert ":" not in slug
        assert ".." not in slug


def test_slug_handles_chinese_and_empty():
    """中文标题保留；全符号标题退化为 note（不能是空文件名）。"""
    assert notes.slugify("项目入口约定") == "项目入口约定"
    assert notes.slugify("!!!") == "note"


def test_write_creates_frontmatter(tmp_path):
    """生成的笔记有四个 frontmatter 字段 + 正文（AC21）。"""
    home, proj = _stores(tmp_path)
    notes.apply([_update()], home, proj, now=FIXED)

    path = proj / "提交前跑-ruff.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    for key in ("type:", "title:", "created:", "updated:"):
        assert key in text
    assert "正文" in text


def test_write_preserves_created_on_update(tmp_path):
    """更新同一笔记时 created 不变、updated 变——「什么时候学会的」有意义。"""
    home, proj = _stores(tmp_path)
    notes.apply([_update()], home, proj, now=FIXED)

    later = datetime(2026, 10, 25, 9, 0, 0)
    notes.apply([_update(body="新正文")], home, proj, now=later)

    text = (proj / "提交前跑-ruff.md").read_text(encoding="utf-8")
    assert f"created: {FIXED.isoformat()}" in text
    assert f"updated: {later.isoformat()}" in text
    assert "新正文" in text


def test_index_prepends_new_entry(tmp_path):
    """索引新条目插最前（位置即优先级，裁剪时先裁尾部）。(D11)"""
    home, proj = _stores(tmp_path)
    notes.apply([_update(title="第一条")], home, proj, now=FIXED)
    notes.apply([_update(title="第二条")], home, proj, now=FIXED)

    lines = (proj / "index.md").read_text(encoding="utf-8").splitlines()
    assert "第二条" in lines[0]
    assert "第一条" in lines[1]


def test_index_no_duplicate_lines(tmp_path):
    """同一文件重复写入 → 索引里只有一行（AC23）。"""
    home, proj = _stores(tmp_path)
    for _ in range(3):
        notes.apply([_update()], home, proj, now=FIXED)

    lines = (proj / "index.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_delete_removes_file_and_index(tmp_path):
    """delete 动作：文件消失、索引行消失。"""
    home, proj = _stores(tmp_path)
    notes.apply([_update()], home, proj, now=FIXED)
    notes.apply(
        [_update(action="delete", file="提交前跑-ruff.md")], home, proj, now=FIXED
    )

    assert not (proj / "提交前跑-ruff.md").exists()
    assert (proj / "index.md").read_text(encoding="utf-8").strip() == ""


def test_user_vs_project_target(tmp_path):
    """target=user 写用户级，project 写项目级，互不串位（AC25）。"""
    home, proj = _stores(tmp_path)
    notes.apply(
        [_update(target="user", title="全局偏好"), _update(title="项目知识")],
        home,
        proj,
        now=FIXED,
    )

    assert (home / "全局偏好.md").exists()
    assert (proj / "项目知识.md").exists()
    assert not (home / "项目知识.md").exists()
    assert not (proj / "全局偏好.md").exists()


def test_missing_dir_created(tmp_path):
    """记忆目录不存在时自动创建（首次使用不该手工 mkdir）。"""
    home = tmp_path / "deep" / "home_mem"
    proj = tmp_path / "deep" / "proj_mem"
    notes.apply([_update()], home, proj, now=FIXED)

    assert (proj / "index.md").exists()


def test_write_failure_returns_warning(tmp_path, monkeypatch):
    """写失败 → 返回告警、不抛（笔记是增强，不是前提）(AC30)。"""
    home, proj = _stores(tmp_path)

    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)

    warnings = notes.apply([_update()], home, proj, now=FIXED)

    assert warnings and "写入失败" in warnings[0]


def test_four_types_written(tmp_path):
    """四类各能写入，frontmatter 的 type 与内容相符（AC22）。"""
    home, proj = _stores(tmp_path)
    updates = [
        _update(type=t, title=f"标题{i}", file="")
        for i, t in enumerate(
            [
                "user_preference",
                "correction_feedback",
                "project_knowledge",
                "reference_material",
            ]
        )
    ]
    notes.apply(updates, home, proj, now=FIXED)

    for i, t in enumerate(
        [
            "user_preference",
            "correction_feedback",
            "project_knowledge",
            "reference_material",
        ]
    ):
        text = (proj / f"标题{i}.md").read_text(encoding="utf-8")
        assert f"type: {t}" in text


def test_empty_update_writes_nothing(tmp_path):
    """模型回 [] → 不建文件、不动索引（AC21）。

    「这一轮没有值得记的」是最常见的输出。若空列表也去写索引，
    每轮都会留下一次无意义的重写，缓存与 mtime 全被打乱。
    """
    home, proj = _stores(tmp_path)
    notes.apply([], home, proj, now=FIXED)

    assert list(proj.glob("*.md")) == []
    assert not (proj / "index.md").exists()


def test_duplicate_title_keeps_single_file(tmp_path):
    """同一标题写三次 → 目录里只有一个 .md，不是三个（AC23/AC28）。"""
    home, proj = _stores(tmp_path)
    for _ in range(3):
        notes.apply([_update()], home, proj, now=FIXED)

    assert [p.name for p in proj.glob("*.md") if p.name != "index.md"] == [
        "提交前跑-ruff.md"
    ]


def test_index_line_uses_description_not_filename(tmp_path):
    """索引行第三段是「一句话描述」，不是 slug 文件名（F8）。

    文件名是 `提交前跑-ruff.md`，模型只能猜出「大概跟 ruff 有关」；
    描述是「提交前必须跑 ruff」，它才能判断要不要展开这条笔记。
    """
    home, proj = _stores(tmp_path)
    notes.apply([_update(description="提交前必须跑 ruff")], home, proj, now=FIXED)

    line = (proj / "index.md").read_text(encoding="utf-8").strip()
    assert "提交前必须跑 ruff" in line
    assert ".md" not in line
