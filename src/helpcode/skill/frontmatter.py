"""Skill frontmatter 解析与参数占位符替换。

frontmatter 是文件开头的 ``---`` 包裹的 YAML；之后是发给模型的 SOP 正文。
解析只做「能读出来的就读出来，读不出来就跳过」——缺关键字段、坏 YAML 一律
返回 ``None``，由加载层跳过并告警，绝不在这里抛异常阻断整体（F4）。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .types import ExecutionMode, Skill

# 占位符：{{args}} 是整段参数，{{1}}..{{N}} 是按空白切分后的第 N 个参数。
_ARGS_RE = re.compile(r"\{\{args\}\}")
_POS_RE = re.compile(r"\{\{(\d+)\}\}")


def substitute(body: str, args: str) -> str:
    """把 SOP 正文里的占位符替换成用户传入的参数。

    - ``{{args}}`` → 整段参数（含内部空格）。
    - ``{{1}}``、``{{2}}``… → 按空白切分后的第 N 个参数。
    - 没有对应参数时，占位符保留原样（不吞字，用户一眼能看出没传对）。
    """
    tokens = args.split() if args.strip() else []
    text = _ARGS_RE.sub(args, body)

    def _pos(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return tokens[idx - 1] if 1 <= idx <= len(tokens) else match.group(0)

    return _POS_RE.sub(_pos, text)


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """切出 ``(frontmatter_yaml, body)``；没有合法的 frontmatter 返回 ``None``。

    统一换行是必需的：Windows 上编辑器存出来的文件默认 CRLF，``---`` 行尾带
    ``\\r`` 会让按行判断失灵，导致整个 frontmatter 静默失效（同 ch09 教训）。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    return None


def _to_mode(value: object) -> ExecutionMode:
    for mode in ExecutionMode:
        if value == mode.value:
            return mode
    return ExecutionMode.SHARED


def _to_tools(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return tuple(out)


def _to_history(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def parse_skill(
    text: str,
    *,
    source: str = "builtin",
    path: Path | None = None,
    directory: Path | None = None,
) -> Skill | None:
    """把一份 Skill 文本解析成 ``Skill``；解析失败返回 ``None``。

    失败场景（由调用方决定告警措辞）：无 frontmatter、缺 name/description、
    YAML 非法、frontmatter 非 mapping。
    """
    split = _split_frontmatter(text)
    if split is None:
        return None
    fm_text, body = split
    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None

    name = meta.get("name")
    description = meta.get("description")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(description, str) or not description.strip():
        return None

    return Skill(
        name=name.strip().lower(),
        description=description.strip(),
        body=body.strip(),
        tools=_to_tools(meta.get("tools")),
        mode=_to_mode(meta.get("mode")),
        history=_to_history(meta.get("history")),
        model=meta.get("model") if isinstance(meta.get("model"), str) else None,
        source=source,
        path=path,
        directory=directory,
    )
