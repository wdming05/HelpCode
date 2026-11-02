"""项目指令文件：三层加载、`@` 引用展开、渲染。

三条设计约束：

1. **任何失败都保留原文**。读不出来、越界、缺失、环路——一律把 `@xxx` 那行原样
   留在结果里并告警，不删除。删掉的话用户看不出问题，只会觉得「我写的那行怎么没了」。
2. **单份裁剪互不影响**。某一份超 32 KB 只裁它自己，不挤占另外两份的额度。
3. **渲染要能被缓存**。`render()` 的输出会进 system prompt 的 stable 段，所以它必须
   是确定性的：同样的输入，逐字节同样的输出（不含时间戳、不含随机顺序）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .constants import BINARY_SNIFF_BYTES, MAX_INCLUDE_DEPTH, MAX_INSTRUCTION_BYTES

# 整行 `@path`。两条约束缺一不可：
#
# 1. **锚定整行**：行内 `@` 在 Markdown 里遍地都是（email、`@dataclass`、
#    社交账号），行内匹配必然误伤。
# 2. **token 必须含 `.` 或 `/`**：光锚定整行还不够——单独一行的 `@dataclass`
#    （代码块被复制出来时很常见）仍会被当成「引用名为 dataclass 的文件」。
#    要求像路径，才能同时放过 `@docs/style.md` 和 `@dataclass`。
#    代价：不支持无扩展名的被引文件（`@Makefile`）。指令文件基本都是 .md，
#    这个取舍换来的是「不会把装饰器当文件」。
_INCLUDE_RE = re.compile(r"^[ \t]*@(\S*[./]\S*)[ \t]*$", re.MULTILINE)

_TRUNCATE_NOTE = "<!-- 内容过长，已截断 -->"


@dataclass(frozen=True)
class InstructionLayer:
    """一层指令文件（已展开、已截断）。"""

    label: str  # 例如 "AGENTS.md"、".helpcode/AGENTS.md"
    path: Path
    text: str
    truncated: bool = False


@dataclass
class InstructionSet:
    """三层加载的汇总结果。"""

    layers: list[InstructionLayer] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        """拼接成注入 system 的文本；全空返回空串。"""
        blocks: list[str] = []
        for layer in self.layers:
            if not layer.text.strip():
                continue
            blocks.append(f"<!-- 来源: {layer.label} -->\n{layer.text.strip()}")
        return "\n\n".join(blocks)


def _read_text(
    path: Path,
    warnings: list[str],
    *,
    max_bytes: int = MAX_INSTRUCTION_BYTES,
) -> tuple[str | None, bool]:
    """读一份指令文件。

    返回 ``(text, truncated)``；``text is None`` 表示这份文件不可用（缺失 /
    二进制 / 编码错误 / 权限不足），调用方应跳过它。

    为什么返回元组而不是单个字符串：``truncated`` 要进 ``InstructionLayer`` 供
    上层展示，而它无法从文本本身推断（截断后的文本和原文没有形态差异）。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None, False  # 不存在或不可访问：正常状态，不告警

    if size == 0:
        return "", False  # 空文件：算「读到了空内容」，不算失败

    try:
        raw = path.read_bytes()
    except OSError as exc:
        warnings.append(f"{path}: 无法读取（{exc.__class__.__name__}），已跳过")
        return None, False

    # 先嗅探二进制：这比等 UnicodeDecodeError 更早给出一条可读的理由，
    # 而且纯 ASCII 的二进制文件不会报编码错误，只会让模型读到一堆乱码。
    if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
        warnings.append(f"{path}: 疑似二进制文件，已跳过")
        return None, False

    truncated = False
    if len(raw) > MAX_INSTRUCTION_BYTES:
        cut = raw[:MAX_INSTRUCTION_BYTES]
        newline = cut.rfind(b"\n")
        raw = cut[:newline] if newline > 0 else cut
        truncated = True

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # 截断点可能把多字节字符切成两半。此时已决定丢弃尾部，
        # 用 ignore 保住前面完整的内容，比整份文件丢弃更合理。
        if truncated:
            text = raw.decode("utf-8", errors="ignore")
        else:
            warnings.append(f"{path}: 不是合法的 UTF-8，已跳过")
            return None, False

    # 统一换行。这一步不是「顺手美化」：Windows 上编辑器存出来的 AGENTS.md
    # 默认是 CRLF，而 `_INCLUDE_RE` 的 `[ \t]*$` 匹配不到行尾那个 \r ——
    # 结果就是 `@文件` **静默地不展开**（连告警都没有），用户只会觉得
    # 「我写的引用怎么没生效」。归一化之后两种换行行为一致。
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    if truncated:
        text = f"{text.rstrip()}\n{_TRUNCATE_NOTE}"
        warnings.append(f"{path}: 超过 {max_bytes // 1024} KB，已截断")

    return text, truncated


def _under(path: Path, root: Path) -> bool:
    """``path`` 是否落在 ``root`` 内（先解析再比，防软链逃逸）。"""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        # 路径含非法字符（如 Windows 的 \\x00）→ 当作越界，走 fail-closed
        return False


def _expand(
    text: str,
    *,
    base_dir: Path,
    boundary: Path,
    depth: int,
    visited: set[Path],
    warnings: list[str],
) -> str:
    """把整行的 ``@路径`` 替换成被引文件正文。

    分支顺序不能换：``深度 → 越界 → 已访问 → 不存在 → 读不出 → 递归``。
    其中「越界」必须排在「已访问」之前——否则同一个文件先被合法引用过一次后，
    再以越界路径引用时会被 ``visited`` 拦住并给出「循环引用」这个错误理由，
    排查时会往完全错误的方向找。
    """

    def replace(match: re.Match[str]) -> str:
        original = match.group(0)
        rel = match.group(1)

        if depth <= 0:
            warnings.append(f"`{rel}`: 嵌套超过 {MAX_INCLUDE_DEPTH} 层，未展开")
            return original

        try:
            target = (base_dir / rel).resolve()
        except (OSError, ValueError):
            warnings.append(f"`{rel}`: 路径非法，未展开")
            return original

        if not _under(target, boundary):
            warnings.append(f"`{rel}`: 超出允许目录，未展开")
            return original

        if target in visited:
            warnings.append(f"`{rel}`: 循环引用，未展开")
            return original

        if not target.exists():
            warnings.append(f"`{rel}`: 文件不存在，未展开")
            return original

        body, _ = _read_text(target, warnings)
        if body is None:
            warnings.append(f"`{rel}`: 无法读取，未展开")
            return original

        visited.add(target)
        return _expand(
            body,
            base_dir=target.parent,
            boundary=boundary,
            depth=depth - 1,
            visited=visited,
            warnings=warnings,
        )

    return _INCLUDE_RE.sub(replace, text)


# 三层的相对路径与「基准目录」归属。
# 顺序即优先级：项目根的 AGENTS.md 排最前（规则吃首因效应）。
LAYERS: tuple[tuple[str, str], ...] = (
    ("AGENTS.md", "root"),
    (".helpcode/AGENTS.md", "root"),
    ("~/.helpcode/AGENTS.md", "home"),
)


def load(
    root: Path,
    home: Path | None = None,
    *,
    max_depth: int = MAX_INCLUDE_DEPTH,
    max_bytes: int = MAX_INSTRUCTION_BYTES,
) -> InstructionSet:
    """按三层优先级加载并展开项目指令文件。

    ``boundary`` 与 ``base_dir`` 是**两个不同的东西**：

    - 项目级：base 是项目根，boundary 也是项目根。
    - 用户级：base 是 ``~/.helpcode``，boundary **也是** ``~/.helpcode``。
      所以用户级文件里的 ``@../project.md`` 会被拦——这是对的：用户级配置
      不该能读取任意主目录下的文件。
    """
    home = home if home is not None else Path.home()
    root = root.resolve()
    home_cfg = (home / ".helpcode").resolve()

    result = InstructionSet()

    for rel, base_kind in LAYERS:
        if base_kind == "root":
            path = (root / rel).resolve()
            base_dir = path.parent
            boundary = root
        else:
            path = home_cfg / Path(rel).name
            base_dir = home_cfg
            boundary = home_cfg

        body, truncated = _read_text(path, result.warnings, max_bytes=max_bytes)
        if body is None:
            continue  # 缺失 / 二进制 / 编码错误：_read_text 已决定是否告警

        expanded = _expand(
            body,
            base_dir=base_dir,
            boundary=boundary,
            depth=max_depth,
            visited={path},
            warnings=result.warnings,
        )
        result.layers.append(
            InstructionLayer(label=rel, path=path, text=expanded, truncated=truncated)
        )

    return result
