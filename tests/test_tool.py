"""工具与注册中心单测。"""

from helpcode.tool import new_default_registry
from helpcode.tool.bash import BashTool
from helpcode.tool.edit_file import EditFileTool
from helpcode.tool.glob_tool import GlobTool
from helpcode.tool.grep_tool import GrepTool
from helpcode.tool.read_file import ReadFileTool
from helpcode.tool.write_file import WriteFileTool


def test_registry_has_six_tools():
    reg = new_default_registry()
    assert set(reg.names()) == {
        "read_file",
        "write_file",
        "edit_file",
        "bash",
        "glob",
        "grep",
    }


def test_registry_get_by_name():
    reg = new_default_registry()
    assert isinstance(reg.get("read_file"), ReadFileTool)
    assert reg.get("不存在的工具") is None


def test_registry_export():
    reg = new_default_registry()
    exported = reg.export()
    assert len(exported) == 6
    for d in exported:
        assert {"name", "description", "parameters"} <= set(d.keys())


def test_registry_read_only_definitions():
    reg = new_default_registry()
    names = [d["name"] for d in reg.read_only_definitions()]
    assert names == ["read_file", "glob", "grep"]


def test_registry_is_read_only():
    reg = new_default_registry()
    assert reg.is_read_only("read_file") is True
    assert reg.is_read_only("glob") is True
    assert reg.is_read_only("grep") is True
    assert reg.is_read_only("write_file") is False
    assert reg.is_read_only("edit_file") is False
    assert reg.is_read_only("bash") is False
    assert reg.is_read_only("不存在") is False


def test_read_file_with_offset_limit(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("一\n二\n三\n四\n", encoding="utf-8")
    r = ReadFileTool().run(str(f), offset=2, limit=2)
    assert r.ok
    assert "2: 二" in r.content
    assert "3: 三" in r.content
    assert "1: 一" not in r.content


def test_read_file_missing(tmp_path):
    r = ReadFileTool().run(str(tmp_path / "no.txt"))
    assert not r.ok
    assert "不存在" in r.content


def test_read_file_truncate(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("\n".join(str(i) for i in range(10)), encoding="utf-8")
    r = ReadFileTool().run(str(f), limit=3)
    assert "已截断" in r.content


def test_write_file_creates_parent(tmp_path):
    target = tmp_path / "sub" / "dir" / "b.txt"
    r = WriteFileTool().run(str(target), "hello")
    assert r.ok
    assert target.read_text(encoding="utf-8") == "hello"


def test_edit_file_unique_match(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello world\n", encoding="utf-8")
    assert EditFileTool().run(str(f), "world", "python").ok
    assert f.read_text(encoding="utf-8") == "hello python\n"


def test_edit_file_zero_match(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\n", encoding="utf-8")
    r = EditFileTool().run(str(f), "zzz", "y")
    assert not r.ok
    assert "0 次" in r.content


def test_edit_file_multiple_match(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("ababa\n", encoding="utf-8")
    r = EditFileTool().run(str(f), "a", "x")
    assert not r.ok
    assert "3 次" in r.content


def test_bash_echo():
    r = BashTool().run("echo hi")
    assert r.ok
    assert "hi" in r.content
    assert "退出码：0" in r.content


def test_bash_timeout():
    r = BashTool().run("sleep 5", timeout=1)
    assert not r.ok
    assert "超时" in r.content


def test_glob(tmp_path):
    (tmp_path / "x.py").write_text("", encoding="utf-8")
    (tmp_path / "y.txt").write_text("", encoding="utf-8")
    r = GlobTool().run(str(tmp_path / "*.py"))
    assert r.ok
    assert "x.py" in r.content
    assert "y.txt" not in r.content


def test_grep(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\nclass A:\n    pass\n", encoding="utf-8")
    r = GrepTool().run("class", str(tmp_path))
    assert r.ok
    assert "class A:" in r.content
    assert "2:" in r.content
