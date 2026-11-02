"""沙箱测试。覆盖 AC2/F2/N2。"""

from types import SimpleNamespace

from helpcode.permission.sandbox import extract_target, inside


def _call(name, **kw):
    return SimpleNamespace(name=name, input=kw)


def test_inside_allows(tmp_path):
    assert inside(str(tmp_path), "src/x.py")[0] is True


def test_inside_rejects_parent(tmp_path):
    assert inside(str(tmp_path), "../outside")[0] is False


def test_inside_rejects_absolute_outside(tmp_path):
    assert inside(str(tmp_path), "/etc/passwd")[0] is False


def test_inside_symlink_escape(tmp_path):
    import pytest

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        (root / "link").symlink_to(outside)
    except OSError:
        pytest.skip("当前环境不支持创建符号链接")
    if not (root / "link").is_symlink():
        # Windows 可能把目录符号链接静默降级为 junction，resolve() 不解析 junction
        pytest.skip("符号链接未真正创建（Windows 降级为 junction），跳过防逃逸断言")
    assert inside(str(root), str(root / "link" / "secret.txt"))[0] is False


def test_inside_missing_intermediate_dirs(tmp_path):
    # 多级未创建中间目录：root/src/a/b/c.py 且 src/a/b 不存在 → 放行
    root = tmp_path / "root"
    root.mkdir()
    assert inside(str(root), str(root / "src" / "a" / "b" / "c.py"))[0] is True


def test_extract_target_file():
    assert extract_target(_call("read_file", path="a.txt")) == ("a.txt", True, True)


def test_extract_target_glob_default():
    assert extract_target(_call("glob")) == (".", True, True)


def test_extract_target_bash():
    assert extract_target(_call("bash", command="echo hi")) == ("echo hi", False, True)


def test_extract_target_missing_path():
    assert extract_target(_call("read_file")) == ("", True, False)


def test_extract_target_unknown():
    assert extract_target(_call("unknown_tool")) == ("", False, True)
