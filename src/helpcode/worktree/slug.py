"""Worktree slug 校验与命名规则。

名字来自模型（LLM 可能被诱导写 ``../../etc``）与用户输入，必须先过这一层
再拼路径：这里错了，后面再多的 git 参数校验都只是补丁。
"""

from __future__ import annotations

import re

# 单段允许的字符：字母、数字、点、下划线、连字符。
# 故意不含空格与反斜杠——反斜杠在 Windows 上会被当分隔符，混进来就绕过了
# 「按 / 切段」的检查。
SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

MAX_LEN = 64
BRANCH_PREFIX = "worktree-"


class SlugError(ValueError):
    """slug 非法。消息里带具体原因，直接可以展示给用户/模型。"""


def validate_slug(name: str) -> str:
    """校验并返回去首尾空白的 slug；非法抛 :class:`SlugError`。"""
    slug = (name or "").strip()
    if not slug:
        raise SlugError("名字不能为空")
    if len(slug) > MAX_LEN:
        raise SlugError(f"名字长度不能超过 {MAX_LEN}（当前 {len(slug)}）")

    segments = slug.split("/")
    for index, seg in enumerate(segments, start=1):
        if not seg:
            raise SlugError("不允许空段：连续 / 或首尾出现 /")
        if seg in (".", ".."):
            raise SlugError(f"第 {index} 段不能是 {seg!r}")
        if not SEGMENT_RE.match(seg):
            raise SlugError(
                f"第 {index} 段 {seg!r} 含非法字符（只允许字母、数字、. _ -）"
            )
    return slug


def flat_slug(name: str) -> str:
    """嵌套分隔的 ``/`` 换成 ``+``，得到目录名。

    Git 里 ``fix`` 与 ``fix/a`` 会形成 D/F 冲突（同名既是目录又是分支路径的
    一段），扁平化从根上避免这类问题。
    """
    return name.replace("/", "+")


def branch_for(name: str) -> str:
    """分支名：固定前缀 + 扁平 slug。前缀让 ``git branch`` 里一眼能认出临时分支。"""
    return BRANCH_PREFIX + flat_slug(name)
