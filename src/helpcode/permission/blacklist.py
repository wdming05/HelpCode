"""危险命令黑名单（内置正则，不可配置、不可绕过）。"""

import re

# 递归强删：rm 带 r/f 类 flag，目标为根目录或敏感前缀
_RM_SENSITIVE = re.compile(
    r"\brm\b(?:\s+-[a-zA-Z]+)+(?:\s+--no-preserve-root)?"
    r"\s+/(?:home|etc|usr|var|tmp|root|boot|srv|opt|proc|sys|dev)(?:/|\s|$|;|&|\|)",
    re.IGNORECASE,
)
# 递归强删根目录本身：rm -rf /（末尾或接控制符）
_RM_ROOT = re.compile(
    r"\brm\b(?:\s+-[a-zA-Z]+)+(?:\s+--no-preserve-root)?\s+/\s*(?:$|;|&|\|)",
    re.IGNORECASE,
)
# 写块设备：dd ... of=/dev/ 或 > /dev/sd*
_DD_DEV = re.compile(r"\bdd\b[^\n]*\bof=/(?:dev|proc|sys)/", re.IGNORECASE)
_REDIR_DEV = re.compile(r">\s*/dev/(?:sd[a-z]|nvme\d+n\d+|hd[a-z])")
# fork 炸弹
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;")
# 格式化文件系统
_MKFS = re.compile(r"\b(?:mkfs|mke2fs)(?:\.\w+)?\b", re.IGNORECASE)

BLACKLIST: list[re.Pattern] = [
    _RM_SENSITIVE,
    _RM_ROOT,
    _DD_DEV,
    _REDIR_DEV,
    _FORK_BOMB,
    _MKFS,
]


def matches(command: str) -> str | None:
    """命中返回可读片段，否则 None。"""
    for pattern in BLACKLIST:
        m = pattern.search(command)
        if m:
            return m.group(0).strip()
    return None
