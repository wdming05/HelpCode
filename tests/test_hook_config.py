"""hooks.yaml 加载与校验。"""

from pathlib import Path

from helpcode.hook.config import load_hooks


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _base(name="h", event="Notification", action_type="prompt", extra=""):
    return f"""
hooks:
  - name: {name}
    event: {event}
    action:
      type: {action_type}
      prompt: hi
{extra}
"""


def test_missing_file_empty(tmp_path):
    hooks, warns = load_hooks(tmp_path, tmp_path / "home")
    assert hooks == [] and warns == []


def test_two_layers_merged(tmp_path):
    project = tmp_path / ".helpcode" / "hooks.yaml"
    user = tmp_path / "home" / ".helpcode" / "hooks.yaml"
    _write(project, _base("p"))
    _write(user, _base("u"))
    hooks, warns = load_hooks(tmp_path, tmp_path / "home")
    assert {h.name for h in hooks} == {"p", "u"}
    assert warns == []


def test_duplicate_name_skipped(tmp_path):
    project = tmp_path / ".helpcode" / "hooks.yaml"
    user = tmp_path / "home" / ".helpcode" / "hooks.yaml"
    _write(user, _base("same"))
    _write(project, _base("same"))
    hooks, warns = load_hooks(tmp_path, tmp_path / "home")
    assert [h.name for h in hooks] == ["same"]
    assert any("重复" in w for w in warns)


def test_missing_name(tmp_path):
    _write(
        tmp_path / ".helpcode" / "hooks.yaml",
        "hooks:\n  - event: Notification\n    action:\n      type: prompt\n      prompt: hi\n",
    )
    hooks, warns = load_hooks(tmp_path, tmp_path / "home")
    assert hooks == [] and any("name" in w for w in warns)


def test_bad_event(tmp_path):
    _write(tmp_path / ".helpcode" / "hooks.yaml", _base(event="Bogus"))
    hooks, warns = load_hooks(tmp_path, tmp_path / "home")
    assert hooks == [] and any("事件名非法" in w for w in warns)


def test_bad_action(tmp_path):
    _write(
        tmp_path / ".helpcode" / "hooks.yaml",
        "hooks:\n  - name: x\n    event: Notification\n    action:\n      type: magic\n",
    )
    hooks, warns = load_hooks(tmp_path, tmp_path / "home")
    assert hooks == [] and any("动作类型非法" in w for w in warns)


def test_intercept_async_rejected(tmp_path):
    _write(
        tmp_path / ".helpcode" / "hooks.yaml",
        """
hooks:
  - name: x
    event: PreToolUse
    async: true
    action:
      type: command
      command: "exit 0"
""",
    )
    hooks, warns = load_hooks(tmp_path, tmp_path / "home")
    assert hooks == [] and any("不允许 async" in w for w in warns)


def test_timeout_parse(tmp_path):
    _write(
        tmp_path / ".helpcode" / "hooks.yaml",
        _base("t", event="Notification") + "    timeout: 5s\n",
    )
    hooks, _ = load_hooks(tmp_path, tmp_path / "home")
    assert hooks[0].timeout == 5.0


def test_timeout_illegal_falls_back(tmp_path):
    _write(
        tmp_path / ".helpcode" / "hooks.yaml",
        _base("t", event="Notification") + "    timeout: abc\n",
    )
    hooks, _ = load_hooks(tmp_path, tmp_path / "home")
    assert hooks[0].timeout == 30.0


def test_mixed_logic_rejected(tmp_path):
    _write(
        tmp_path / ".helpcode" / "hooks.yaml",
        """
hooks:
  - name: x
    event: Notification
    if:
      all_of: [{field: a, pattern: "=1"}]
      any_of: [{field: b, pattern: "=2"}]
    action:
      type: prompt
      prompt: hi
""",
    )
    hooks, warns = load_hooks(tmp_path, tmp_path / "home")
    assert hooks == [] and any("混用" in w or "all_of" in w for w in warns)


def test_bad_regex_condition(tmp_path):
    _write(
        tmp_path / ".helpcode" / "hooks.yaml",
        """
hooks:
  - name: x
    event: Notification
    if:
      field: tool
      pattern: "~[unclosed"
    action:
      type: prompt
      prompt: hi
""",
    )
    hooks, warns = load_hooks(tmp_path, tmp_path / "home")
    assert hooks == [] and any("模式非法" in w for w in warns)
