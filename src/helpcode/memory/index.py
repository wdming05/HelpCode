"""记忆索引：读取、双上限裁剪、渲染成注入文本。

索引是「笔记的目录」，不是笔记本身。它会被整体注入 system prompt，所以必须有
硬上限——否则用户攒了三百条笔记，每轮请求都在为它们付 token。

两个上限同时生效，先到先算：

- **行数**：200 行。一条笔记一行，这是「能看多少条」。
- **字节**：25 KB。防止「200 行但每行很长」的极端情况。

裁剪方向是**保留前 N 行**。索引由 ``notes`` 维护成「新条目插最前」，所以
前 N 行就是最近更新的 N 条 —— 位置即优先级，不需要解析日期。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .constants import MAX_INDEX_BYTES, MAX_INDEX_LINES

_USER_HEADER = "### 用户级记忆"
_PROJECT_HEADER = "### 项目级记忆"
_CROPPED_TMPL = "<!-- 索引已裁剪：另有 {n} 条笔记未列出，可按需检索 -->"


@dataclass
class IndexBundle:
    user: str = ""
    project: str = ""
    dropped: int = 0
    warnings: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.user.strip() and not self.project.strip()


def cap(
    text: str,
    *,
    max_lines: int = MAX_INDEX_LINES,
    max_bytes: int = MAX_INDEX_BYTES,
) -> tuple[str, int]:
    """裁剪到行数/字节双上限内，返回 ``(结果, 丢弃行数)``。

    两步顺序不能反：先按行粗裁（便宜，O(1) 定位），再按字节精裁。
    反过来先算字节，遇到 200 行也得逐行累加，白白多扫一遍。
    """
    lines = text.splitlines(keepends=True)
    dropped = 0

    if len(lines) > max_lines:
        dropped += len(lines) - max_lines
        lines = lines[:max_lines]

    # 字节上限：逐行累加，超了就停。用 utf-8 编码长度而非 len(str)，
    # 因为限制的是「发给 API 的字节量」，中文一个字 3 字节。
    kept: list[str] = []
    total = 0
    for line in lines:
        size = len(line.encode("utf-8"))
        if total + size > max_bytes:
            break
        kept.append(line)
        total += size

    dropped += len(lines) - len(kept)
    return "".join(kept), dropped


def _read_index(path: Path, warnings: list[str]) -> str:
    """读一份索引文件。缺失或空 → 空串（正常状态，不告警）。"""
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        warnings.append(f"{path}: 索引读取失败（{exc.__class__.__name__}），已跳过")
        return ""


def load(
    home_memory: Path,
    project_memory: Path,
    *,
    max_lines: int = MAX_INDEX_LINES,
    max_bytes: int = MAX_INDEX_BYTES,
) -> IndexBundle:
    """加载两级索引并各自裁剪。"""
    bundle = IndexBundle()

    user_raw = _read_index(home_memory / "index.md", bundle.warnings)
    proj_raw = _read_index(project_memory / "index.md", bundle.warnings)

    bundle.user, user_dropped = cap(user_raw, max_lines=max_lines, max_bytes=max_bytes)
    bundle.project, proj_dropped = cap(
        proj_raw, max_lines=max_lines, max_bytes=max_bytes
    )
    bundle.dropped = user_dropped + proj_dropped
    return bundle


def render(bundle: IndexBundle) -> str:
    """渲染成注入文本：用户级在前、项目级在后。

    为什么用户级在前：注进 system 后是「指令 → 记忆」，记忆内部再分两级。
    项目级更贴近当前工作，放在**后**面（靠近对话）吃近因效应，与指令层的
    「项目在前」正好相反 —— 这是有意的，见 plan「注入顺序与裁剪归属」。
    """
    blocks: list[str] = []
    if bundle.user.strip():
        blocks.append(f"{_USER_HEADER}\n{bundle.user.strip()}")
    if bundle.project.strip():
        blocks.append(f"{_PROJECT_HEADER}\n{bundle.project.strip()}")

    text = "\n\n".join(blocks)
    if text and bundle.dropped > 0:
        text = f"{text}\n\n{_CROPPED_TMPL.format(n=bundle.dropped)}"
    return text
