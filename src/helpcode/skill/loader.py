"""三级发现与加载、白名单校验。

三级优先级：项目（最高）> 用户 > 内置（最低）。同名按优先级覆盖。
单个文件解析失败跳过并告警，不阻断整体（F4）。
"""

from __future__ import annotations

from pathlib import Path

from .builtin import BUILTIN_SKILLS
from .frontmatter import parse_skill
from .registry import SkillRegistry
from .types import Skill, WhitelistError

# 目录型 Skill 的入口文件名
ENTRY_NAME = "SKILL.md"


def load_skills(root: Path, home: Path | None = None) -> SkillRegistry:
    """按三级加载全部 Skill，返回注册中心（告警挂在 registry.warnings 之外返回）。

    三级顺序即优先级：先写内置（低），再写用户，最后写项目（高），
    ``SkillRegistry.set`` 的同名覆盖保证高优先级生效。
    """
    home = home if home is not None else Path.home()
    root = root.resolve()

    registry = SkillRegistry()
    registry.warnings = []

    for skill in BUILTIN_SKILLS:
        registry.set(skill)

    user_dir = (home / ".helpcode" / "skills").resolve()
    for skill, warn in _load_from_dir(user_dir, source="user"):
        if warn is not None:
            registry.warnings.append(warn)
        elif skill is not None:
            registry.set(skill)

    proj_dir = (root / ".helpcode" / "skills").resolve()
    for skill, warn in _load_from_dir(proj_dir, source="project"):
        if warn is not None:
            registry.warnings.append(warn)
        elif skill is not None:
            registry.set(skill)

    return registry


def _load_from_dir(
    directory: Path, *, source: str
) -> list[tuple[Skill | None, str | None]]:
    """扫描一个层级目录，返回 ``(skill, warning)`` 列表（二者最多一个非空）。"""
    if not directory.is_dir():
        return []

    seen: set[str] = set()
    out: list[tuple[Skill | None, str | None]] = []

    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            entry_file = entry / ENTRY_NAME
            if not entry_file.is_file():
                continue
            skill = _parse_file(entry_file, source=source, directory=entry)
        elif entry.suffix == ".md":
            skill = _parse_file(entry, source=source)
        else:
            continue

        if skill is None:
            out.append((None, f"{entry}: 解析失败，已跳过"))
            continue
        if skill.name in seen:
            out.append((None, f"{entry}: 名字 /{skill.name} 在本层重复，已跳过"))
            continue
        seen.add(skill.name)
        out.append((skill, None))

    return out


def _parse_file(path: Path, *, source: str, directory: Path | None = None):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_skill(text, source=source, path=path, directory=directory)


def validate_whitelists(registry: SkillRegistry, known_tool_names) -> None:
    """校验所有 Skill 的白名单都指向真实存在的工具；否则抛 WhitelistError。

    这是「启动时白名单里出现不存在的工具就立刻报错」（F10）：宁可启动就停，
    也不要等模型选到那个工具才发现它不存在。
    """
    known = set(known_tool_names)
    for skill in registry.all():
        for tool in skill.tools:
            if tool not in known:
                raise WhitelistError(skill.name, tool)
