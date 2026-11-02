"""五层权限判定流水线。"""

import re
from dataclasses import dataclass
from pathlib import Path

from .blacklist import BLACKLIST
from .blacklist import matches as _blacklist_matches
from .config import load_default_mode, load_rulesets
from .decision import Decision
from .modes import Category, Mode, fallback
from .rules import RuleSet, friendly_name
from .sandbox import extract_target, inside

# 唯一会被黑名单检查的内置工具名（MCP 工具即使同名参数也不受黑名单约束）
BASH_TOOL = "bash"


@dataclass
class Engine:
    root: str  # 绝对、已 resolve 的项目根
    blacklist: list[re.Pattern]
    user: RuleSet
    project: RuleSet
    local: RuleSet
    local_path: str
    start_mode: Mode


def categorize(internal: str, read_only: bool) -> Category:
    """判定工具类别；read_only 优先，未知非只读归 EXEC。"""
    if read_only:
        return Category.READ
    if internal in ("read_file", "glob", "grep"):
        return Category.READ
    if internal in ("write_file", "edit_file"):
        return Category.WRITE
    return Category.EXEC  # bash 及未知非只读


def new_engine(root: str) -> tuple[Engine, Exception | None]:
    """构造引擎，永不抛、永不返回 None。

    唯一返回非 None err 的情形是 resolve_root 失败；三层配置文件的
    read/解析失败只降级该文件为空规则集，绝不向上抛致命异常。
    """
    try:
        root_abs = str(Path(root).resolve())
    except (OSError, ValueError) as e:
        eng = Engine(
            root=root,
            blacklist=list(BLACKLIST),
            user=RuleSet(),
            project=RuleSet(),
            local=RuleSet(),
            local_path=str(Path(root) / ".helpcode" / "settings.local.yaml"),
            start_mode=Mode.DEFAULT,
        )
        return eng, e

    user, project, local = load_rulesets(root_abs)
    start_mode = load_default_mode(root_abs)
    eng = Engine(
        root=root_abs,
        blacklist=list(BLACKLIST),
        user=user,
        project=project,
        local=local,
        local_path=str(Path(root_abs) / ".helpcode" / "settings.local.yaml"),
        start_mode=start_mode,
    )
    return eng, None


def check(engine: Engine, mode: Mode, call, read_only: bool) -> tuple[Decision, str]:
    """五层判定：黑名单→沙箱→规则(local>project>user)→模式兜底。纯同步。"""
    category = categorize(call.name, read_only)
    friendly = friendly_name(call.name)

    # ① 黑名单（仅内置 bash；MCP 等外部工具即使有同名 command 参数也不在此列）
    if category is Category.EXEC and call.name == BASH_TOOL:
        command = (call.input or {}).get("command")
        command = command if isinstance(command, str) else ""
        if command:
            hit = _blacklist_matches(command)
            if hit is not None:
                return Decision.DENY, f"命中危险命令黑名单：{hit}"

    # ② 沙箱（仅文件类）
    if category in (Category.READ, Category.WRITE):
        target, _is_file, ok = extract_target(call)
        if not ok:
            return Decision.DENY, "无法解析文件路径参数，安全拒绝"
        inside_ok, reason = inside(engine.root, target)
        if not inside_ok:
            return Decision.DENY, reason

    # ③ 规则引擎（local > project > user，就近命中即止）
    matched = _match_target(engine, call, category)
    for ruleset in (engine.local, engine.project, engine.user):
        decision, hit = ruleset.match(friendly, matched)
        if hit:
            if decision is Decision.DENY:
                return Decision.DENY, f"匹配 deny 规则：{friendly}({matched})"
            return Decision.ALLOW, ""

    # ④ 模式兜底（只产 ALLOW/ASK）
    fb = fallback(mode, category)
    if fb is Decision.ALLOW:
        return Decision.ALLOW, ""
    return Decision.ASK, f"{mode} 模式下 {_category_cn(category)} 类操作需确认"


def _match_target(engine: Engine, call, category: Category) -> str:
    """规则匹配对象：命令执行比对命令串，文件类比项目相对路径。"""
    target, is_file, _ok = extract_target(call)
    if is_file and target:
        p = Path(target)
        if p.is_absolute():
            try:
                return str(p.resolve().relative_to(Path(engine.root).resolve()))
            except ValueError:
                return target
    return target


def _category_cn(category: Category) -> str:
    return {
        Category.READ: "只读",
        Category.WRITE: "文件写",
        Category.EXEC: "命令执行",
    }[category]
