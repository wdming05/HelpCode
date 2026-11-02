"""权限匹配器扩展：精确 / 反向 / 正则 / glob。"""

import pytest

from helpcode.permission.pattern import PatternError, compile_pattern
from helpcode.permission.rules import Rule, RuleSet, parse_rule


def test_exact_matcher():
    m = compile_pattern("=git status")
    assert m.matches("git status")
    assert not m.matches("git status --short")


def test_regex_matcher():
    m = compile_pattern(r"~^rm\s+-rf")
    assert m.matches("rm -rf /tmp/x")
    assert not m.matches("rm file")


def test_not_matcher():
    m = compile_pattern("!git *")
    assert m.matches("ls -la")
    assert not m.matches("git push")


def test_glob_matcher():
    m = compile_pattern("git *")
    assert m.matches("git push")
    assert not m.matches("ls")


def test_nested_not():
    assert compile_pattern("!=x").matches("y")
    assert not compile_pattern("!=x").matches("x")
    assert compile_pattern("!~^rm").matches("ls")
    assert not compile_pattern("!~^rm").matches("rm -rf x")


def test_bad_regex_raises():
    with pytest.raises(PatternError):
        compile_pattern("~[unclosed")


def test_parse_rule_compiles():
    r = parse_rule("Bash(=git status)")
    assert r.pattern == "=git status"
    assert r.matcher is not None and r.matcher.matches("git status")


def test_glob_rule_still_works():
    r = parse_rule("Bash(git *)")
    assert r.pattern == "git *"  # 原始串保留（向后兼容）
    rs = RuleSet(allow=[r])
    assert rs.match("Bash", "git push")[0].name == "ALLOW"
    assert rs.match("Bash", "ls")[1] is False


def test_direct_rule_still_matches():
    # 不经过 parse_rule 直接构造（无 matcher）也走旧 glob 语义
    rs = RuleSet(allow=[Rule("Bash", "git *", True)])
    from helpcode.permission.decision import Decision

    assert rs.match("Bash", "git push") == (Decision.ALLOW, True)


def test_exact_rule_via_ruleset():
    rs = RuleSet(deny=[parse_rule("Bash(=rm -rf /)", allow=False)])
    from helpcode.permission.decision import Decision

    assert rs.match("Bash", "rm -rf /")[0] is Decision.DENY
    assert rs.match("Bash", "rm -rf /tmp")[1] is False


def test_bad_rule_skipped(tmp_path, monkeypatch, capfd):
    """配置层：坏正则规则被跳过、stderr 有告警，其余规则正常加载（F4）。"""
    from helpcode.permission import config

    monkeypatch.setattr(config, "_user_path", lambda: tmp_path / "nouser.yaml")
    (tmp_path / ".helpcode").mkdir()
    (tmp_path / ".helpcode" / "settings.yaml").write_text(
        """
permissions:
  allow:
    - 'Bash(~[unclosed)'
    - 'Bash(git *)'
""",
        encoding="utf-8",
    )
    _user, project, _local = config.load_rulesets(str(tmp_path))
    assert any(r.pattern == "git *" for r in project.allow)
    assert not any(r.pattern == "~[unclosed" for r in project.allow)
    assert "解析失败" in capfd.readouterr().err
