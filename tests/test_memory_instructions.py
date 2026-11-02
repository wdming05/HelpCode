"""单文件读取：空 / 二进制 / 编码错误 / 超限截断。"""

from pathlib import Path

import pytest

from helpcode.memory import instructions as ins
from helpcode.memory.constants import MAX_INSTRUCTION_BYTES


def _read(tmp_path: Path, content: bytes | str, name: str = "AGENTS.md"):
    p = tmp_path / name
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    warnings: list[str] = []
    text, truncated = ins._read_text(p, warnings)
    return text, truncated, warnings


def test_empty_file_skipped(tmp_path):
    """空文件返回空串（不是 None）——它算「读到了空内容」，由上层过滤空段。"""
    text, truncated, warnings = _read(tmp_path, "")
    assert text == ""
    assert truncated is False
    assert warnings == []


def test_binary_file_skipped(tmp_path):
    """前 512 字节含 \\x00 → 判为二进制，跳过并告警（AC35）。"""
    text, truncated, warnings = _read(
        tmp_path, b"\x89PNG\r\n\x1a\n\x00\x00\x01\x02", name="x.png"
    )
    assert text is None
    assert truncated is False
    assert any("二进制" in w for w in warnings)


def test_bad_encoding_skipped(tmp_path):
    """非 UTF-8 且不含 \\x00 → 编码错误，跳过并告警。"""
    text, _truncated, warnings = _read(tmp_path, "中文".encode("gbk"))
    assert text is None
    assert any("UTF-8" in w for w in warnings)


def test_oversize_truncated_with_warning(tmp_path):
    """超限 → 截断到上限内、truncated 为真、有告警、末尾有截断说明。"""
    big = ("行内容\n" * (MAX_INSTRUCTION_BYTES // 4)) * 2
    text, truncated, warnings = _read(tmp_path, big)

    assert truncated is True
    assert text is not None
    assert len(text.encode()) <= MAX_INSTRUCTION_BYTES + 64  # 说明行本身占几字节
    assert text.rstrip().endswith(ins._TRUNCATE_NOTE)
    assert any("截断" in w for w in warnings)


def test_truncate_on_line_boundary(tmp_path):
    """截断点回退到上一个换行，不能把一个字符切成两半。"""
    # 每行 3 字节 + 1 换行；总长刚过上限
    big = "abc\n" * (MAX_INSTRUCTION_BYTES // 4 + 10)
    text, truncated, _ = _read(tmp_path, big)

    assert truncated is True
    body = text.rsplit(ins._TRUNCATE_NOTE, 1)[0]
    assert body.endswith("abc\n")  # 以完整行结尾
    assert "\ufffd" not in body  # 没有替换字符


def test_missing_file_returns_none(tmp_path):
    """不存在 → None，且**不告警**：缺文件是正常状态（第一层往往没有）。"""
    warnings: list[str] = []
    text, truncated = ins._read_text(tmp_path / "nope.md", warnings)
    assert text is None
    assert truncated is False
    assert warnings == []


def test_multibyte_at_cut_is_ignored_not_fatal(tmp_path):
    """截断点落在多字节字符中间：保住前面内容，不因半字符丢掉整份文件。"""
    # 让第 MAX_INSTRUCTION_BYTES 个字节落在某个汉字中间
    head = "a" * (MAX_INSTRUCTION_BYTES - 2)
    p = tmp_path / "AGENTS.md"
    p.write_bytes(head.encode("utf-8") + "中".encode())

    warnings: list[str] = []
    text, truncated = ins._read_text(p, warnings)
    assert truncated is True
    assert text is not None
    assert text.startswith("a" * 100)


def test_oserror_on_read_warns(tmp_path, monkeypatch):
    """读的过程中抛 OSError（如权限变更）→ 跳过 + 告警，不向上抛。"""
    p = tmp_path / "AGENTS.md"
    p.write_text("x", encoding="utf-8")

    def boom(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", boom)
    warnings: list[str] = []
    text, _ = ins._read_text(p, warnings)
    assert text is None
    assert any("无法读取" in w for w in warnings)


@pytest.mark.parametrize("name", [".helpcode/AGENTS.md", "~/.helpcode/AGENTS.md"])
def test_read_text_is_path_agnostic(tmp_path, name):
    """三层用同一个函数：路径形状不影响读取行为。"""
    p = tmp_path / Path(name).name
    p.write_text("内容", encoding="utf-8")
    assert ins._read_text(p, [])[0] == "内容"


# ---------------------------------------------------------------- `@` 展开


def _expand(tmp_path, content: str, *, boundary=None, depth=None):
    """在 tmp_path 里写一个主文件并展开它，返回 (结果, 告警)。"""
    from helpcode.memory.constants import MAX_INCLUDE_DEPTH

    p = tmp_path / "AGENTS.md"
    p.write_text(content, encoding="utf-8")
    warnings: list[str] = []
    out = ins._expand(
        content,
        base_dir=tmp_path,
        boundary=boundary or tmp_path,
        depth=MAX_INCLUDE_DEPTH if depth is None else depth,
        visited={p.resolve()},
        warnings=warnings,
    )
    return out, warnings


def test_inline_at_not_expanded(tmp_path):
    """行内 @ 一律不动：email、装饰器、句中提及（D2）。"""
    (tmp_path / "x.md").write_text("不该出现", encoding="utf-8")
    content = "联系我 a@b.com\n@dataclass\n见 @x.md 末尾\n"
    out, warnings = _expand(tmp_path, content)

    assert out == content
    assert warnings == []


def test_simple_include(tmp_path):
    (tmp_path / "docs.md").write_text("正文内容", encoding="utf-8")
    out, warnings = _expand(tmp_path, "前言\n@docs.md\n后记")
    assert "正文内容" in out
    assert "@docs.md" not in out
    assert warnings == []


def test_relative_base_is_declaring_file(tmp_path):
    """被引文件里的相对引用，基准是**它自己所在目录**，不是项目根。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text("@b.md", encoding="utf-8")
    (sub / "b.md").write_text("子目录里的内容", encoding="utf-8")

    out, _ = _expand(tmp_path, "@sub/a.md")
    assert "子目录里的内容" in out


def test_depth_limit(tmp_path):
    """6 层链：第 6 个文件的引用不再展开，且保留原文 + 告警（AC5）。"""
    for i in range(6):
        (tmp_path / f"L{i}.md").write_text(f"L{i}\n@L{i + 1}.md", encoding="utf-8")
    (tmp_path / "L6.md").write_text("L6", encoding="utf-8")

    out, warnings = _expand(tmp_path, "@L0.md")

    # depth=5 只能展开 5 次：L0→L1→L2→L3→L4，第 6 个文件（L5）进不来
    assert "L4" in out
    assert "@L5.md" in out  # 那一行保留原文
    assert "L5\n" not in out.replace("@L5.md", "")
    assert any("嵌套" in w for w in warnings)


def test_cycle_detected(tmp_path):
    """A 引 B、B 引 A → 第二次遇到 A 保留原文 + 告警，不无限递归（AC6）。"""
    (tmp_path / "a.md").write_text("A\n@b.md", encoding="utf-8")
    (tmp_path / "b.md").write_text("B\n@a.md", encoding="utf-8")

    out, warnings = _expand(tmp_path, "@a.md")

    assert "A" in out and "B" in out
    assert "@a.md" in out
    assert any("循环" in w for w in warnings)


def test_escape_blocked(tmp_path):
    """`@../outside.md` → 不展开、保留原文、告警（AC7）。"""
    outside = tmp_path.parent / "outside.md"
    outside.write_text("机密", encoding="utf-8")

    out, warnings = _expand(tmp_path, "@../outside.md")

    assert out == "@../outside.md"
    assert "机密" not in out
    assert any("超出允许目录" in w for w in warnings)


def test_missing_include_keeps_original(tmp_path):
    """被引文件不存在 → 那一行原样留下 + 告警，不删除。"""
    out, warnings = _expand(tmp_path, "前\n@nope.md\n后")
    assert "@nope.md" in out
    assert any("不存在" in w for w in warnings)


def test_escape_checked_before_visited(tmp_path):
    """已访问过的文件再被越界引用：告警理由是「越界」，不是「循环」。"""
    from helpcode.memory.constants import MAX_INCLUDE_DEPTH

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.md").write_text("A", encoding="utf-8")
    warnings: list[str] = []
    a = (tmp_path / "sub" / "a.md").resolve()

    ins._expand(
        "@../sub/a.md",
        base_dir=tmp_path / "sub",
        boundary=tmp_path,
        depth=MAX_INCLUDE_DEPTH,
        visited={a},  # 已访问
        warnings=warnings,
    )
    assert any("循环" in w for w in warnings)

    warnings.clear()
    ins._expand(
        "@../outside.md",
        base_dir=tmp_path / "sub",
        boundary=tmp_path / "sub",  # 收紧边界，使 ../sub/a.md 也越界
        depth=MAX_INCLUDE_DEPTH,
        visited={a},
        warnings=warnings,
    )
    assert any("超出允许目录" in w for w in warnings)
    assert not any("循环" in w for w in warnings)


# ---------------------------------------------------------------- 三层加载


def _tree(tmp_path):
    """造 (root, home)，并返回三者路径。"""
    root = tmp_path / "proj"
    home = tmp_path / "home"
    (root / ".helpcode").mkdir(parents=True)
    (home / ".helpcode").mkdir(parents=True)
    return root, home


def test_three_layers_order(tmp_path):
    """三层都有时，顺序必须是 项目根 → .helpcode → home（位置断言，不是 in）。"""
    root, home = _tree(tmp_path)
    (root / "AGENTS.md").write_text("一层", encoding="utf-8")
    (root / ".helpcode" / "AGENTS.md").write_text("二层", encoding="utf-8")
    (home / ".helpcode" / "AGENTS.md").write_text("三层", encoding="utf-8")

    text = ins.load(root, home).render()

    assert text.index("一层") < text.index("二层") < text.index("三层")


def test_missing_layers_skipped(tmp_path):
    """只有一层存在 → 结果里只有一层，不报错。"""
    root, home = _tree(tmp_path)
    (root / ".helpcode" / "AGENTS.md").write_text("唯一", encoding="utf-8")

    result = ins.load(root, home)

    assert len(result.layers) == 1
    assert result.layers[0].label == ".helpcode/AGENTS.md"
    assert "唯一" in result.render()


def test_empty_set_renders_empty(tmp_path):
    """三层都不存在 → render() 返回空串（不是空行、不是空白）。"""
    root, home = _tree(tmp_path)
    result = ins.load(root, home)
    assert result.layers == []
    assert result.render() == ""


def test_render_has_source_marker(tmp_path):
    """每段都带来源标记，且标记数与层数一致。"""
    root, home = _tree(tmp_path)
    (root / "AGENTS.md").write_text("A", encoding="utf-8")
    (home / ".helpcode" / "AGENTS.md").write_text("C", encoding="utf-8")

    text = ins.load(root, home).render()

    assert text.count("<!-- 来源:") == 2
    assert "<!-- 来源: AGENTS.md -->" in text
    assert "<!-- 来源: ~/.helpcode/AGENTS.md -->" in text


def test_no_blank_section(tmp_path):
    """空文件不产生空段：标记行数 == 有内容的层数。"""
    root, home = _tree(tmp_path)
    (root / "AGENTS.md").write_text("有内容", encoding="utf-8")
    (root / ".helpcode" / "AGENTS.md").write_text("   \n\n  ", encoding="utf-8")

    text = ins.load(root, home).render()

    assert text.count("<!-- 来源:") == 1
    assert "\n\n\n" not in text


def test_warnings_aggregated(tmp_path):
    """多层的告警汇总到同一个列表里，带路径便于定位。"""
    root, home = _tree(tmp_path)
    (root / "AGENTS.md").write_text("@nope1.md", encoding="utf-8")
    (root / ".helpcode" / "AGENTS.md").write_text("@nope2.md", encoding="utf-8")

    result = ins.load(root, home)

    assert len(result.warnings) == 2
    assert all("不存在" in w for w in result.warnings)


def test_include_inside_layer_uses_that_layer_boundary(tmp_path):
    """用户级文件不能引用到项目根（各层边界独立）。"""
    root, home = _tree(tmp_path)
    (root / "secret.md").write_text("项目机密", encoding="utf-8")
    (home / ".helpcode" / "AGENTS.md").write_text(
        "@../../proj/secret.md", encoding="utf-8"
    )

    result = ins.load(root, home)

    assert "项目机密" not in result.render()
    assert any("超出允许目录" in w for w in result.warnings)


def test_load_is_deterministic(tmp_path):
    """同样输入两次加载，render() 逐字节相同（N1 要求可缓存）。"""
    root, home = _tree(tmp_path)
    (root / "AGENTS.md").write_text("稳定内容\n@sub.md", encoding="utf-8")
    (root / "sub.md").write_text("被引", encoding="utf-8")

    assert ins.load(root, home).render() == ins.load(root, home).render()


@pytest.mark.parametrize("token", ["@../../etc/passwd", "@C://Windows//win.ini"])
def test_absolute_and_deep_escape_blocked(tmp_path, token):
    """多级上跳与绝对路径都不展开（AC7/N4）。

    POSIX 上 `C://Windows//win.ini` 会被当普通文件名（不存在），Windows 上会被
    认成绝对路径——两条路都必须是「保留原文 + 告警」，不能有一条直接放行。
    """
    out, warnings = _expand(tmp_path, token)

    assert out == token
    assert warnings


def test_crlf_file_still_expands(tmp_path):
    """CRLF 换行的指令文件也要展开 `@`（Windows 保存出来的默认换行）。

    这条必须走真实文件：直接给 `_expand` 传字符串永远测不出来——字符串里是
    `\n`，落盘再读回来才是 `\r\n`。早先的用例全是传字符串，于是
    「Windows 上 @ 引用静默失效」这个 bug 一路绿灯到了端到端。
    """
    root, home = _tree(tmp_path)
    (root / "docs").mkdir()
    (root / "docs" / "STYLE.md").write_bytes(b"CRLF_BODY")
    (root / "AGENTS.md").write_bytes("规则\r\n@docs/STYLE.md\r\n".encode())

    result = ins.load(root, home)

    assert "CRLF_BODY" in result.render()
    assert result.warnings == []
