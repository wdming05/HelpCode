"""三层 YAML 配置加载与降级。"""

import sys
from pathlib import Path

import yaml

from .modes import Mode, parse_mode
from .pattern import PatternError
from .rules import RuleSet, parse_rule


class SettingsError(Exception):
    """配置文件解析错误。"""


def _user_path() -> Path:
    return Path.home() / ".helpcode" / "settings.yaml"


def _project_path(root: str) -> Path:
    return Path(root) / ".helpcode" / "settings.yaml"


def _local_path(root: str) -> Path:
    return Path(root) / ".helpcode" / "settings.local.yaml"


def _load_file(path: Path) -> dict | None:
    """加载单个文件；缺失返回 None，格式非法抛 SettingsError。"""
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SettingsError(f"YAML 解析失败: {path}") from e
    except OSError as e:
        raise SettingsError(f"读取失败: {path}") from e
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SettingsError(f"配置顶层必须是 mapping: {path}")
    return raw


def _ruleset_from(raw: dict | None, source: Path | None = None) -> RuleSet:
    """从配置 dict 构造 RuleSet；缺失/非法降级为空。

    单条规则解析失败（如正则编译错误）打印到 stderr 并跳过该条，
    其余规则正常加载（F4，不再静默）。
    """
    if not raw:
        return RuleSet()
    perms = raw.get("permissions")
    if not isinstance(perms, dict):
        return RuleSet()
    allow = perms.get("allow") or []
    deny = perms.get("deny") or []
    where = f"{source} " if source is not None else ""
    allow_rules = _parse_rules(allow, True, where)
    deny_rules = _parse_rules(deny, False, where)
    return RuleSet(allow=allow_rules, deny=deny_rules)


def _parse_rules(items, allow: bool, where: str) -> list:
    """逐条解析；坏规则打印 stderr 并跳过，不影响其余。"""
    rules = []
    for s in items:
        if not isinstance(s, str):
            continue
        try:
            rules.append(parse_rule(s, allow=allow))
        except PatternError as e:
            print(
                f"[warn] {where}权限规则解析失败（已跳过）：{s} — {e}", file=sys.stderr
            )
    return rules


def load_rulesets(root: str) -> tuple[RuleSet, RuleSet, RuleSet]:
    """返回 (user, project, local) 三级规则集；缺失/非法降级为空（不抛）。"""
    result: list[RuleSet] = []
    for path in (_user_path(), _project_path(root), _local_path(root)):
        try:
            raw = _load_file(path)
        except SettingsError:
            raw = None  # 降级为空，不抛
        result.append(_ruleset_from(raw, path))
    return result[0], result[1], result[2]


def load_default_mode(root: str) -> Mode:
    """default_mode 按 local > project > user 生效，皆无则 DEFAULT。"""
    for path in (_local_path(root), _project_path(root), _user_path()):
        try:
            raw = _load_file(path)
        except SettingsError:
            raw = None
        if raw:
            dm = raw.get("default_mode")
            if isinstance(dm, str):
                mode, ok = parse_mode(dm)
                if ok:
                    return mode
    return Mode.DEFAULT
