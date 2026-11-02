"""配置加载测试。覆盖 AC6/AC18/F4/N5。"""

from helpcode.permission.config import load_default_mode, load_rulesets
from helpcode.permission.modes import Mode


def test_missing_files_empty(tmp_path):
    u, p, l = load_rulesets(str(tmp_path))
    assert u.allow == [] and u.deny == []
    assert p.allow == [] and p.deny == []
    assert l.allow == [] and l.deny == []


def test_invalid_yaml_degraded(tmp_path):
    d = tmp_path / ".helpcode"
    d.mkdir()
    (d / "settings.yaml").write_text(":: invalid: [", encoding="utf-8")
    _u, p, _l = load_rulesets(str(tmp_path))  # 不抛，降级为空
    assert p.allow == []


def test_default_mode_priority(tmp_path):
    d = tmp_path / ".helpcode"
    d.mkdir()
    (d / "settings.yaml").write_text("default_mode: acceptEdits\n", encoding="utf-8")
    (d / "settings.local.yaml").write_text(
        "default_mode: bypassPermissions\n", encoding="utf-8"
    )
    assert load_default_mode(str(tmp_path)) == Mode.BYPASS


def test_default_mode_default_when_absent(tmp_path):
    assert load_default_mode(str(tmp_path)) == Mode.DEFAULT


def test_rulesets_load(tmp_path):
    d = tmp_path / ".helpcode"
    d.mkdir()
    (d / "settings.yaml").write_text(
        "permissions:\n  allow:\n    - Bash(git *)\n  deny:\n    - Bash(rm *)\n",
        encoding="utf-8",
    )
    _u, p, _l = load_rulesets(str(tmp_path))
    assert any(r.pattern == "git *" for r in p.allow)
    assert any(r.pattern == "rm *" for r in p.deny)
