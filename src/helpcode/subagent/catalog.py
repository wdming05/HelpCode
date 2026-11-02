"""子 Agent 角色定义：Markdown + YAML frontmatter，三级加载与同名覆盖。

层次（低 → 高）：内置 → 用户 ``~/.helpcode/agents/`` → 项目 ``<root>/.helpcode/agents/``。
同名按 source 优先级覆盖。单文件解析失败只告警跳过，不阻断启动（F5）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .builtin import BUILTIN_MD, FORK_MD

NAME_RE = re.compile(r"^[a-z0-9-]{1,32}$")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk")
DEFAULT_PERMISSION_MODE = "default"
# ch14：隔离模式。"" = 共享主仓库工作目录；"worktree" = 每个子 Agent 一个独立 Worktree
ISOLATION_MODES = ("", "worktree")


@dataclass(frozen=True)
class Role:
    """一个子 Agent 角色定义。"""

    name: str
    description: str
    body: str  # 子 Agent 系统提示
    tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    model: str = "inherit"
    max_turns: int | None = None
    permission_mode: str = DEFAULT_PERMISSION_MODE
    background: bool = False
    isolation: str = ""  # ch14："" | "worktree"
    source: str = "builtin"  # builtin / user / project
    path: Path | None = None


class Catalog:
    """已加载角色集合；同名覆盖（高优先级后写入）。"""

    def __init__(self) -> None:
        self._roles: dict[str, Role] = {}
        self._order: list[str] = []
        self.warnings: list[str] = []

    def set(self, role: Role) -> None:
        if role.name not in self._roles:
            self._order.append(role.name)
        self._roles[role.name] = role

    def get(self, name: str) -> Role | None:
        return self._roles.get(name.strip().lower())

    def all(self) -> list[Role]:
        return [self._roles[n] for n in self._order]

    def fork_base(self) -> Role:
        role, _err = parse_role(FORK_MD, source="builtin")
        assert role is not None
        return role

    def __len__(self) -> int:
        return len(self._order)


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """统一换行后切出 (yaml, body)；无合法 frontmatter 返回 None。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    return None


def _to_str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v).strip() for v in value if isinstance(v, str) and v.strip())


def _to_max_turns(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def parse_role(
    text: str,
    *,
    source: str = "builtin",
    path: Path | None = None,
) -> tuple[Role | None, str | None]:
    """解析一份角色文本；失败返回 ``(None, 原因)``。"""
    split = _split_frontmatter(text)
    if split is None:
        return None, "缺少 frontmatter"
    fm_text, body = split
    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return None, "frontmatter YAML 非法"
    if not isinstance(meta, dict):
        return None, "frontmatter 必须是 mapping"

    name = meta.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name.strip()):
        return None, "name 缺失或非法（需 [a-z0-9-]{1,32}）"
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return None, "缺少 description"

    mode = meta.get("permissionMode", DEFAULT_PERMISSION_MODE)
    if mode not in PERMISSION_MODES:
        mode = DEFAULT_PERMISSION_MODE

    # 非法隔离值回落到共享目录：隔离是安全增强项，拼错字不该让角色整个失效，
    # 但一定要留痕（调用方从 role.isolation 能看出被降级了）
    isolation = str(meta.get("isolation") or "").strip()
    if isolation not in ISOLATION_MODES:
        isolation = ""

    return (
        Role(
            name=name.strip(),
            description=description.strip(),
            body=body.strip(),
            tools=_to_str_tuple(meta.get("tools")),
            disallowed_tools=_to_str_tuple(meta.get("disallowedTools")),
            model=str(meta.get("model") or "inherit"),
            max_turns=_to_max_turns(meta.get("maxTurns")),
            permission_mode=mode,
            background=bool(meta.get("background", False)),
            isolation=isolation,
            source=source,
            path=path,
        ),
        None,
    )


def load_catalog(root: Path, home: Path | None = None) -> tuple[Catalog, list[str]]:
    """三级加载角色，返回 (catalog, warnings)。"""
    home = home if home is not None else Path.home()
    root = root.resolve()
    catalog = Catalog()
    warnings: list[str] = []

    for md in BUILTIN_MD:
        role, err = parse_role(md, source="builtin")
        if role is not None:
            catalog.set(role)
        else:
            warnings.append(f"内置角色解析失败：{err}")

    for directory, source in (
        ((home / ".helpcode" / "agents").resolve(), "user"),
        ((root / ".helpcode" / "agents").resolve(), "project"),
    ):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as e:
                warnings.append(f"{path}: 读取失败（{e}），已跳过")
                continue
            role, err = parse_role(text, source=source, path=path)
            if role is None:
                warnings.append(f"{path}: {err}，已跳过")
                continue
            catalog.set(role)

    catalog.warnings = warnings
    return catalog, warnings
