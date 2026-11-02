"""三级优先级测试。覆盖 AC5/F4。"""

from types import SimpleNamespace

from helpcode.permission import Decision, Mode, check
from helpcode.permission.blacklist import BLACKLIST
from helpcode.permission.engine import Engine
from helpcode.permission.rules import Rule, RuleSet


def _call(name, **kw):
    return SimpleNamespace(name=name, input=kw)


def _engine(user, project, local):
    return Engine(
        root=".",
        blacklist=list(BLACKLIST),
        user=user,
        project=project,
        local=local,
        local_path="",
        start_mode=Mode.DEFAULT,
    )


def test_local_overrides_project():
    eng = _engine(
        user=RuleSet(),
        project=RuleSet(deny=[Rule("Write", "x.py", False)]),
        local=RuleSet(allow=[Rule("Write", "x.py", True)]),
    )
    # local allow 覆盖 project deny（个人机器最终话语权）
    assert (
        check(eng, Mode.DEFAULT, _call("write_file", path="x.py"), False)[0]
        == Decision.ALLOW
    )


def test_project_overrides_user():
    eng = _engine(
        user=RuleSet(allow=[Rule("Bash", "git *", True)]),
        project=RuleSet(deny=[Rule("Bash", "git push", False)]),
        local=RuleSet(),
    )
    assert (
        check(eng, Mode.DEFAULT, _call("bash", command="git push"), False)[0]
        == Decision.DENY
    )


def test_same_layer_deny_first():
    eng = _engine(
        user=RuleSet(),
        project=RuleSet(),
        local=RuleSet(
            allow=[Rule("Bash", "git *", True)], deny=[Rule("Bash", "git push", False)]
        ),
    )
    assert (
        check(eng, Mode.DEFAULT, _call("bash", command="git push"), False)[0]
        == Decision.DENY
    )


def test_nearest_hit_stops():
    # project deny 命中即止，不再看 user 的 allow
    eng = _engine(
        user=RuleSet(allow=[Rule("Bash", "git *", True)]),
        project=RuleSet(deny=[Rule("Bash", "git push", False)]),
        local=RuleSet(),
    )
    assert (
        check(eng, Mode.DEFAULT, _call("bash", command="git push"), False)[0]
        == Decision.DENY
    )
