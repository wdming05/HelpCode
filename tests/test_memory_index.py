"""索引裁剪与渲染：200 行 / 25 KB 双上限。"""

from pathlib import Path

from helpcode.memory import index as idx
from helpcode.memory.constants import MAX_INDEX_BYTES, MAX_INDEX_LINES


def _lines(n: int) -> str:
    return "".join(f"- 笔记 {i}\n" for i in range(n))


def test_cap_by_lines():
    """250 行 → 剩 200 行，丢弃 50 行（AC27）。"""
    text = _lines(250)
    out, dropped = idx.cap(text, max_lines=200, max_bytes=10**9)

    assert len(out.splitlines()) == 200
    assert dropped == 50
    assert "笔记 0" in out
    assert "笔记 249" not in out  # 保留的是前排（最近更新的）


def test_cap_by_bytes():
    """一行 30 KB：行数没超也要按字节截断（AC27）。"""
    text = "x" * (30 * 1024) + "\n"
    out, dropped = idx.cap(text, max_lines=200, max_bytes=MAX_INDEX_BYTES)

    assert len(out.encode("utf-8")) <= MAX_INDEX_BYTES
    assert dropped == 1


def test_cap_bytes_is_utf8_aware():
    """中文按 3 字节算：同样的「字数」，中文能留下的行更少。"""
    ascii_text = "a" * 100 + "\n"
    cjk_text = "中" * 100 + "\n"

    out_ascii, _ = idx.cap(ascii_text * 10, max_lines=200, max_bytes=500)
    out_cjk, _ = idx.cap(cjk_text * 10, max_lines=200, max_bytes=500)

    assert len(out_ascii.splitlines()) > len(out_cjk.splitlines())


def test_cap_no_change_when_small():
    """小索引原样返回，dropped 为 0。"""
    text = _lines(3)
    out, dropped = idx.cap(text)
    assert out == text
    assert dropped == 0


def test_cap_keeps_newest_entries():
    """倒序维护：裁剪后留下的是文件开头那批（最新），不是结尾（最旧）。(D11)"""
    text = "".join(f"- 第{i}条\n" for i in range(300))
    out, _ = idx.cap(text, max_lines=MAX_INDEX_LINES, max_bytes=10**9)

    assert "第0条" in out
    assert "第298条" not in out


def _bundle(tmp_path: Path, user: str = "", project: str = "") -> idx.IndexBundle:
    home = tmp_path / "home_mem"
    proj = tmp_path / "proj_mem"
    home.mkdir(exist_ok=True)
    proj.mkdir(exist_ok=True)
    if user:
        (home / "index.md").write_text(user, encoding="utf-8")
    if project:
        (proj / "index.md").write_text(project, encoding="utf-8")
    return idx.load(home, proj)


def test_render_order_user_first(tmp_path):
    """用户级在前、项目级在后，各带层级标记（AC26）。"""
    bundle = _bundle(tmp_path, user="- 用户偏好\n", project="- 项目知识\n")
    text = idx.render(bundle)

    assert text.index(idx._USER_HEADER) < text.index(idx._PROJECT_HEADER)
    assert text.index("用户偏好") < text.index("项目知识")


def test_cropped_notice_appended(tmp_path):
    """有裁剪时末尾有说明行，且说明里的数字与 dropped 一致（AC27）。"""
    bundle = _bundle(tmp_path, project=_lines(250))
    text = idx.render(bundle)

    assert bundle.dropped == 50
    assert "另有 50 条笔记未列出" in text


def test_no_cropped_notice_when_intact(tmp_path):
    """没裁剪时不该有说明行。"""
    bundle = _bundle(tmp_path, user="- 一条\n")
    assert "已裁剪" not in idx.render(bundle)


def test_missing_index_skipped(tmp_path):
    """两级目录都不存在 → 空 bundle、render 为空、不告警（读路径无副作用）。"""
    empty = tmp_path / "nope"
    bundle = idx.load(empty / "a", empty / "b")

    assert bundle.is_empty()
    assert idx.render(bundle) == ""
    assert bundle.warnings == []


def test_empty_file_treated_as_missing(tmp_path):
    """index.md 存在但为空 → 视为空，不产生空段、不告警。"""
    home = tmp_path / "hm"
    proj = tmp_path / "pm"
    home.mkdir()
    proj.mkdir()
    (home / "index.md").write_text("", encoding="utf-8")

    bundle = idx.load(home, proj)

    assert bundle.is_empty()
    assert bundle.warnings == []


def test_two_levels_capped_independently(tmp_path):
    """两级各自 200 行上限，不是合计 200（AC25/AC27）。"""
    bundle = _bundle(tmp_path, user=_lines(200), project=_lines(200))
    text = idx.render(bundle)

    assert bundle.dropped == 0
    assert text.count("笔记 0") == 2  # 两级各自的第一条都在


def test_bad_encoding_index_warns(tmp_path):
    """索引文件编码错误 → 跳过 + 告警，不影响另一级。"""
    home = tmp_path / "hm"
    proj = tmp_path / "pm"
    home.mkdir()
    proj.mkdir()
    (home / "index.md").write_bytes("中文".encode("gbk"))
    (proj / "index.md").write_text("- 项目条目\n", encoding="utf-8")

    bundle = idx.load(home, proj)

    assert bundle.user == ""
    assert "项目条目" in bundle.project
    assert any("索引读取失败" in w for w in bundle.warnings)
