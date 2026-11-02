"""结构化匹配器：精确 / 反向 / 正则 / glob。

ch08 的权限规则原本只支持单一通配。这里把它扩展成四种可组合的匹配类型，
供权限规则与 Hook 条件共用（F1–F3）。

语法（写在规则串的括号里）：
- ``=value``  精确：整串相等
- ``~regex``  正则：``re.search``，加载期编译
- ``!inner``  反向：对 inner 取反，inner 自身仍按本规则解析（支持 ``!=x`` / ``!~re`` / ``!glob``）
- ``value``   无前缀：沿用 glob 语义
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch

EXACT = "exact"
NOT = "not"
REGEX = "regex"
GLOB = "glob"

KINDS = (EXACT, NOT, REGEX, GLOB)


class PatternError(ValueError):
    """模式非法（目前只有正则编译失败会触发）。"""


@dataclass(frozen=True)
class Matcher:
    """一个已编译的匹配器。"""

    kind: str
    value: str = ""
    regex: re.Pattern | None = None
    inner: Matcher | None = None

    def matches(self, target: str) -> bool:
        if self.kind == EXACT:
            return target == self.value
        if self.kind == GLOB:
            # 沿用 ch08 的 fnmatch 语义（注意它内部做 normcase，
            # Windows 上大小写不敏感——这是既有行为，保持不变）
            return fnmatch(target, self.value)
        if self.kind == REGEX:
            return bool(self.regex and self.regex.search(target))
        if self.kind == NOT:
            return not (self.inner is not None and self.inner.matches(target))
        return False


def compile_pattern(spec: str) -> Matcher:
    """把规则串编译成 Matcher；正则编译失败抛 PatternError。"""
    text = spec.strip()
    if text.startswith("!"):
        return Matcher(NOT, inner=compile_pattern(text[1:]))
    if text.startswith("="):
        return Matcher(EXACT, value=text[1:])
    if text.startswith("~"):
        body = text[1:]
        try:
            return Matcher(REGEX, value=body, regex=re.compile(body))
        except re.error as e:
            raise PatternError(f"正则编译失败：{body}（{e}）") from e
    return Matcher(GLOB, value=text)
