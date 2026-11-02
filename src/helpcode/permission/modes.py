"""权限模式、工具类别与模式兜底表。"""

from enum import Enum, IntEnum

from .decision import Decision


class Mode(IntEnum):
    """权限模式（四档）。"""

    DEFAULT = 0
    ACCEPT_EDITS = 1
    PLAN = 2
    BYPASS = 3

    def __str__(self) -> str:
        return {
            Mode.DEFAULT: "default",
            Mode.ACCEPT_EDITS: "acceptEdits",
            Mode.PLAN: "plan",
            Mode.BYPASS: "bypassPermissions",
        }[self]


def parse_mode(s: str) -> tuple[Mode, bool]:
    """解析模式名（大小写不敏感）；未知返回 (Mode.DEFAULT, False)。"""
    for m in Mode:
        if str(m).lower() == s.lower():
            return m, True
    return Mode.DEFAULT, False


class Category(Enum):
    """工具类别。"""

    READ = "read"
    WRITE = "write"
    EXEC = "exec"


# 模式兜底表：值域严格 {ALLOW, ASK}，永不产 DENY
_FALLBACK: dict[Mode, dict[Category, Decision]] = {
    Mode.DEFAULT: {
        Category.READ: Decision.ALLOW,
        Category.WRITE: Decision.ASK,
        Category.EXEC: Decision.ASK,
    },
    Mode.ACCEPT_EDITS: {
        Category.READ: Decision.ALLOW,
        Category.WRITE: Decision.ALLOW,
        Category.EXEC: Decision.ASK,
    },
    Mode.PLAN: {
        Category.READ: Decision.ALLOW,
        Category.WRITE: Decision.ASK,
        Category.EXEC: Decision.ASK,
    },
    Mode.BYPASS: {
        Category.READ: Decision.ALLOW,
        Category.WRITE: Decision.ALLOW,
        Category.EXEC: Decision.ALLOW,
    },
}


def fallback(mode: Mode, category: Category) -> Decision:
    """规则未命中时的模式兜底裁决，只产 ALLOW/ASK。"""
    return _FALLBACK[mode][category]
