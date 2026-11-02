"""角色目录：解析、三级覆盖、内置角色。"""

from pathlib import Path

from helpcode.subagent.catalog import load_catalog, parse_role


def _md(name="general-purpose", **fm):
    extra = "\n".join(f"{k}: {v}" for k, v in fm.items())
    return f"---\nname: {name}\ndescription: 测试角色\n{extra}\n---\n角色正文\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_parse_full():
    role, err = parse_role(
        _md(
            "helper",
            tools="[read_file, grep]",
            disallowedTools="[bash]",
            model="sonnet",
            maxTurns="10",
            permissionMode="dontAsk",
            background="true",
        )
    )
    assert err is None and role is not None
    assert role.name == "helper"
    assert role.tools == ("read_file", "grep")
    assert role.disallowed_tools == ("bash",)
    assert role.model == "sonnet"
    assert role.max_turns == 10
    assert role.permission_mode == "dontAsk"
    assert role.background is True


def test_missing_name():
    role, err = parse_role("---\ndescription: d\n---\nbody")
    assert role is None and "name" in err


def test_bad_name():
    role, err = parse_role(_md("Bad Name!"))
    assert role is None and "name" in err


def test_bad_frontmatter():
    role, _err = parse_role("no frontmatter")
    assert role is None


def test_bad_mode_falls_back():
    role, _ = parse_role(_md("x", permissionMode="whatever"))
    assert role.permission_mode == "default"


def test_max_turns():
    role, _ = parse_role(_md("x", maxTurns="abc"))
    assert role.max_turns is None
    role2, _ = parse_role(_md("x", maxTurns="7"))
    assert role2.max_turns == 7


def test_builtin_roles():
    cat, warns = load_catalog(Path("."), Path("."))
    names = {r.name for r in cat.all()}
    assert {"general-purpose", "explore", "plan"} <= names
    assert warns == []


def test_fork_base():
    cat, _ = load_catalog(Path("."), Path("."))
    assert cat.fork_base().name == "fork"


def test_three_level_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    _write(home / ".helpcode" / "agents" / "x.md", _md("x"))
    _write(tmp_path / ".helpcode" / "agents" / "x.md", _md("x", model="opus"))

    cat, warns = load_catalog(tmp_path, home)
    role = cat.get("x")
    assert role is not None and role.source == "project"
    assert role.model == "opus"
    assert warns == []


def test_bad_file_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / ".helpcode" / "agents" / "bad.md", "no frontmatter")
    _write(tmp_path / ".helpcode" / "agents" / "good.md", _md("good"))

    cat, warns = load_catalog(tmp_path, tmp_path / "home")
    assert cat.get("good") is not None
    assert any("bad" in w for w in warns)
