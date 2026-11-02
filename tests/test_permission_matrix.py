"""模式矩阵与类别判定测试。覆盖 AC7/AC15/F5/N7。"""

from types import SimpleNamespace

from helpcode.permission import Decision, Mode, categorize, check
from helpcode.permission.blacklist import BLACKLIST
from helpcode.permission.engine import Engine
from helpcode.permission.modes import Category, fallback
from helpcode.permission.rules import RuleSet


def _call(name, **kw):
    return SimpleNamespace(name=name, input=kw)


def _engine():
    return Engine(
        root=".",
        blacklist=list(BLACKLIST),
        user=RuleSet(),
        project=RuleSet(),
        local=RuleSet(),
        local_path="",
        start_mode=Mode.DEFAULT,
    )


def test_fallback_matrix():
    assert fallback(Mode.DEFAULT, Category.READ) == Decision.ALLOW
    assert fallback(Mode.DEFAULT, Category.WRITE) == Decision.ASK
    assert fallback(Mode.DEFAULT, Category.EXEC) == Decision.ASK
    assert fallback(Mode.ACCEPT_EDITS, Category.WRITE) == Decision.ALLOW
    assert fallback(Mode.ACCEPT_EDITS, Category.EXEC) == Decision.ASK
    assert fallback(Mode.PLAN, Category.WRITE) == Decision.ASK
    assert fallback(Mode.BYPASS, Category.WRITE) == Decision.ALLOW
    assert fallback(Mode.BYPASS, Category.EXEC) == Decision.ALLOW


def test_fallback_never_deny():
    for m in Mode:
        for c in Category:
            assert fallback(m, c) in (Decision.ALLOW, Decision.ASK)


def test_categorize():
    assert categorize("read_file", True) == Category.READ
    assert categorize("unknown_tool", True) == Category.READ  # read_only 优先
    assert categorize("write_file", False) == Category.WRITE
    assert categorize("edit_file", False) == Category.WRITE
    assert categorize("bash", False) == Category.EXEC
    assert categorize("unknown_tool", False) == Category.EXEC


def test_unknown_tool_asks_not_deny():
    d, _ = check(_engine(), Mode.DEFAULT, _call("unknown_tool"), False)
    assert d == Decision.ASK  # 未知非只读 → EXEC → Ask，不直接 Deny


def test_read_only_never_ask():
    eng = _engine()
    for mode in Mode:
        d, _ = check(eng, mode, _call("read_file", path="x.py"), True)
        assert d == Decision.ALLOW
