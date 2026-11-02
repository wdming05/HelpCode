"""解析器规则测试。"""

from helpcode.command.parser import parse


def test_plain_text_not_command():
    assert parse("你好") is None
    assert parse("help") is None  # 不带斜杠不算命令


def test_empty_and_blank_not_command():
    assert parse("") is None
    assert parse("   ") is None
    assert parse("\t\n") is None


def test_simple_command():
    p = parse("/plan")
    assert p.name == "plan"
    assert p.args == ""


def test_case_insensitive_name():
    assert parse("/PLAN").name == "plan"
    assert parse("/Plan").name == "plan"
    assert parse("/pLaN").name == "plan"


def test_args_split():
    p = parse("/review src/a.py tests/")
    assert p.name == "review"
    assert p.args == "src/a.py tests/"


def test_args_preserve_inner_spaces():
    p = parse("/review   多个  空格  ")
    assert p.args == "多个  空格"


def test_slash_only_is_empty_name():
    p = parse("/")
    assert p is not None
    assert p.name == ""
    assert p.args == ""


def test_raw_preserved():
    p = parse("/Review  Some Args")
    assert p.raw == "/Review  Some Args"
