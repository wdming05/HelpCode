"""Skill frontmatter 解析与参数占位符替换。"""

from helpcode.skill.frontmatter import parse_skill, substitute
from helpcode.skill.types import ExecutionMode


def _md(body_extra="", **fm):
    fm_lines = [f"{k}: {v}" for k, v in fm.items()]
    return "---\n" + "\n".join(fm_lines) + "\n---\nSOP 正文\n" + body_extra


def test_parse_full_fields():
    skill = parse_skill(
        _md(
            name="review",
            description="审查代码",
            tools="[read_file, grep, bash]",
            mode="isolated",
            history="3",
            model="qwen3.7-max",
        )
    )
    assert skill is not None
    assert skill.name == "review"
    assert skill.description == "审查代码"
    assert skill.body == "SOP 正文"
    assert skill.tools == ("read_file", "grep", "bash")
    assert skill.mode is ExecutionMode.ISOLATED
    assert skill.history == 3
    assert skill.model == "qwen3.7-max"


def test_name_lowercased():
    skill = parse_skill(_md(name="Commit", description="d"))
    assert skill.name == "commit"


def test_missing_name_skipped():
    assert parse_skill(_md(description="d")) is None


def test_missing_description_skipped():
    assert parse_skill(_md(name="x")) is None


def test_bad_yaml_skipped():
    text = "---\nname: [unclosed\n---\nbody"
    assert parse_skill(text) is None


def test_no_frontmatter_skipped():
    assert parse_skill("just body") is None


def test_frontmatter_not_mapping_skipped():
    assert parse_skill("---\n- a\n- b\n---\nbody") is None


def test_crlf_frontmatter():
    text = "---\r\nname: commit\r\ndescription: d\r\n---\r\n正文\r\n"
    skill = parse_skill(text)
    assert skill is not None
    assert skill.name == "commit"
    assert skill.body == "正文"


def test_mode_defaults_shared():
    assert parse_skill(_md(name="x", description="d")).mode is ExecutionMode.SHARED
    assert (
        parse_skill(_md(name="x", description="d", mode="bogus")).mode
        is ExecutionMode.SHARED
    )


def test_history_defaults_zero():
    assert parse_skill(_md(name="x", description="d")).history == 0
    assert parse_skill(_md(name="x", description="d", history="abc")).history == 0
    assert parse_skill(_md(name="x", description="d", history="-5")).history == 0


def test_tools_parsed():
    skill = parse_skill(_md(name="x", description="d", tools="[a, b]"))
    assert skill.tools == ("a", "b")
    # 缺 tools 时是空元组（不限制）
    assert parse_skill(_md(name="x", description="d")).tools == ()


# ---------------------------------------------------------------- substitute


def test_substitute_args():
    assert substitute("范围：{{args}}", "src/foo") == "范围：src/foo"


def test_substitute_positional():
    assert substitute("A {{1}} B {{2}}", "x y z") == "A x B y"


def test_substitute_missing_keeps():
    assert substitute("{{3}} 和 {{args}}", "a b") == "{{3}} 和 a b"
