"""规则引擎测试。覆盖 AC3/AC4/F3。"""

from helpcode.permission.decision import Decision
from helpcode.permission.rules import (
    FRIENDLY_TO_INTERNAL,
    Rule,
    RuleSet,
    escape_glob,
    friendly_name,
    parse_rule,
)


def test_parse_rule_with_pattern():
    r = parse_rule("Bash(git *)")
    assert r.tool == "Bash" and r.pattern == "git *" and r.allow


def test_parse_rule_without_pattern():
    r = parse_rule("Bash")
    assert r.tool == "Bash" and r.pattern is None


def test_parse_rule_deny():
    r = parse_rule("Write(src/**)", allow=False)
    assert r.tool == "Write" and r.pattern == "src/**" and not r.allow


def test_friendly_name_routing():
    assert friendly_name("bash") == "Bash"
    assert friendly_name("read_file") == "Read"
    assert friendly_name("write_file") == "Write"
    assert friendly_name("edit_file") == "Edit"
    assert friendly_name("glob") == "Glob"
    assert friendly_name("grep") == "Grep"
    assert FRIENDLY_TO_INTERNAL["Bash"] == "bash"


def test_match_deny_first():
    rs = RuleSet(
        deny=[Rule("Bash", "git push", False)], allow=[Rule("Bash", "git *", True)]
    )
    assert rs.match("Bash", "git push") == (Decision.DENY, True)
    assert rs.match("Bash", "git status") == (Decision.ALLOW, True)


def test_match_glob():
    rs = RuleSet(allow=[Rule("Write", "src/**", True)])
    assert rs.match("Write", "src/a/b.py") == (Decision.ALLOW, True)
    assert rs.match("Write", "docs/x.py") == (Decision.ALLOW, False)


def test_match_exact():
    rs = RuleSet(allow=[Rule("Bash", "git status", True)])
    assert rs.match("Bash", "git status") == (Decision.ALLOW, True)
    assert rs.match("Bash", "git push") == (Decision.ALLOW, False)


def test_escape_glob():
    assert escape_glob("*.txt") == "[*].txt"
    assert escape_glob("echo *.txt") == "echo [*].txt"


def test_tool_glob_matches_prefixed_name():
    """`*` 匹配任意字符序列，含 `__` 分隔符；deny 与 allow 两侧都要生效。"""
    friendly = "mcp__github__create_issue"
    allow_set = RuleSet(allow=[parse_rule("mcp__github__*")])
    deny_set = RuleSet(deny=[parse_rule("mcp__github__*", allow=False)])
    assert allow_set.match(friendly, "") == (Decision.ALLOW, True)
    assert deny_set.match(friendly, "") == (Decision.DENY, True)


def test_tool_glob_is_case_sensitive():
    """锁死大小写敏感：防止实现被换回 fnmatch（Windows 上会 normcase 转小写）。"""
    rs = RuleSet(allow=[parse_rule("mcp__github__*")])
    assert rs.match("mcp__GitHub__X", "") == (Decision.ALLOW, False)


def test_non_glob_tool_name_still_exact():
    """不含通配符的工具名仍走严格相等，内置工具行为不变。"""
    rs = RuleSet(allow=[parse_rule("Bash(git status)")])
    assert rs.match("Bash", "git status") == (Decision.ALLOW, True)
    assert rs.match("bash", "git status") == (Decision.ALLOW, False)
