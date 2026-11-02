"""explicit cwd：把「这次调用在哪个目录」变成显式参数。

为什么不 chdir：主 Agent 与后台子 Agent 在同一进程里，进程级 cwd 是共享可变
状态——谁 chdir 一下，另一边正在跑的命令就落错地方。所以工作目录随调用传，
不进入任何全局状态。
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_path(path: str, cwd: str | None = None) -> Path:
    """绝对路径原样返回；相对路径拼到 cwd（缺省进程 cwd）上。

    故意不 ``resolve()``：符号链接与 ``..`` 保留调用方写法，
    报错信息里显示的就是用户写的那条路径。
    """
    raw = (path or "").strip()
    p = Path(raw)
    if p.is_absolute():
        return p
    base = cwd or os.getcwd()
    return Path(base) / p
