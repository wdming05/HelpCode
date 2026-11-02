"""权限判定结果与人在回路回传值。"""

from enum import Enum, IntEnum


class Decision(Enum):
    """五层判定的中间值：放行 / 拒绝 / 询问。"""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class Outcome(IntEnum):
    """人在回路三选一的回传值（非裸 Decision）。"""

    DENY_ONCE = 0
    ALLOW_ONCE = 1
    ALLOW_FOREVER = 2
