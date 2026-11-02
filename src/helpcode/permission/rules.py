"""规则引擎：规则解析、友好名路由、精确/glob 匹配。"""

from dataclasses import dataclass, field
from fnmatch import fnmatch, fnmatchcase

from .decision import Decision
from .pattern import Matcher, compile_pattern

# 工具名里出现这些字符才按通配处理，否则走严格相等（保证内置工具行为不变）
_GLOB_CHARS = frozenset("*?[")

# 友好名 ↔ 内置工具名
FRIENDLY_TO_INTERNAL = {
    "Bash": "bash",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "Glob": "glob",
    "Grep": "grep",
}
INTERNAL_TO_FRIENDLY = {v: k for k, v in FRIENDLY_TO_INTERNAL.items()}


def friendly_name(internal: str) -> str:
    """内置工具名 → 友好名；未登记返回原名。"""
    return INTERNAL_TO_FRIENDLY.get(internal, internal)


def escape_glob(s: str) -> str:
    """把 glob 元字符转义为字面（永久放行精确规则用，防止 `*` 被当通配）。"""
    out: list[str] = []
    for ch in s:
        if ch in "*?[]":
            out.append("[" + ch + "]")
        else:
            out.append(ch)
    return "".join(out)


@dataclass
class Rule:
    tool: str
    pattern: str | None
    allow: bool
    # ch12：编译后的匹配器。为 None 时回落旧 glob 语义（直接构造 Rule 的
    # 测试与 persist 不受影响，F5 向后兼容）。
    matcher: Matcher | None = None


@dataclass
class RuleSet:
    allow: list[Rule] = field(default_factory=list)
    deny: list[Rule] = field(default_factory=list)

    def match(self, friendly: str, target: str) -> tuple[Decision, bool]:
        """先 deny 后 allow，命中即返回 (decision, True)；未命中 (ALLOW, False)。"""
        for r in self.deny:
            if _tool_matches(r.tool, friendly) and _rule_matches(r, target):
                return Decision.DENY, True
        for r in self.allow:
            if _tool_matches(r.tool, friendly) and _rule_matches(r, target):
                return Decision.ALLOW, True
        return Decision.ALLOW, False


def _tool_matches(pattern: str, friendly: str) -> bool:
    """工具名匹配：含通配符时走 fnmatchcase，否则严格相等。

    用 ``fnmatchcase`` 而不是 ``fnmatch``——后者内部会做 normcase，
    在 Windows 上把两边都转小写，导致同一份规则文件在两个平台上语义不同。
    """
    if _GLOB_CHARS.isdisjoint(pattern):
        return pattern == friendly
    return fnmatchcase(friendly, pattern)


def _matches(pattern: str | None, target: str) -> bool:
    if pattern is None:
        return True
    return fnmatch(target, pattern)


def _rule_matches(rule: Rule, target: str) -> bool:
    """优先用编译后的 matcher；没有则回落旧 glob 语义（F5）。"""
    if rule.matcher is not None:
        return rule.matcher.matches(target)
    return _matches(rule.pattern, target)


def parse_rule(text: str, allow: bool = True) -> Rule:
    """解析「工具名(模式)」为 Rule；不带模式段表示匹配该工具全部。

    模式在解析期编译成 matcher；正则编译失败抛 ``PatternError``，
    由配置层捕获后打印 stderr 并跳过该条（F4）。
    """
    t = text.strip()
    if "(" in t and t.endswith(")"):
        tool, rest = t.split("(", 1)
        pattern = rest[:-1].strip() or None
    else:
        tool, pattern = t, None
    matcher = compile_pattern(pattern) if pattern is not None else None
    return Rule(tool=tool.strip(), pattern=pattern, allow=allow, matcher=matcher)
