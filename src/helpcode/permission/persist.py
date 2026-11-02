"""永久放行持久化：把精确 allow 规则写入本地层。"""

import logging
from pathlib import Path

import yaml

from .rules import Rule, escape_glob, friendly_name

logger = logging.getLogger(__name__)


def rule_for(call) -> tuple[Rule, str, bool]:
    """生成精确 allow 规则，返回 (Rule, yaml_str, ok)。

    - bash → Bash(<转义命令串>)
    - 文件写/改 → Write(<转义路径>) / Edit(<转义路径>)
    - 解析失败 / 未知工具 → ok=False（调用方跳过写文件）
    """
    name = call.name
    inp = call.input or {}
    if name == "bash":
        command = inp.get("command")
        if not isinstance(command, str) or not command.strip():
            return Rule("Bash", None, True), "", False
        escaped = escape_glob(command.strip())
        return Rule("Bash", escaped, True), f"Bash({escaped})", True
    if name in ("write_file", "edit_file"):
        path = inp.get("path")
        if not isinstance(path, str) or not path.strip():
            return Rule("Write", None, True), "", False
        friendly = friendly_name(name)
        escaped = escape_glob(path.strip())
        return Rule(friendly, escaped, True), f"{friendly}({escaped})", True
    return Rule("", None, True), "", False


def persist_local_allow(engine, call) -> None:
    """生成精确规则 → 追加内存 local 层 → 重写 local 文件。

    先改内存后写文件；写文件前对 allow 去重；异常仅记日志、绝不阻断执行。
    """
    rule, yaml_str, ok = rule_for(call)
    if not ok:
        return
    # 去重：同一规则串不重复追加（幂等）
    if any(
        r.tool == rule.tool and r.pattern == rule.pattern for r in engine.local.allow
    ):
        return
    engine.local.allow.append(rule)

    path = Path(engine.local_path)
    try:
        if path.exists():
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                raw = {}
        else:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        perms = raw.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
        allow = perms.get("allow")
        if not isinstance(allow, list):
            allow = []
        if yaml_str not in allow:
            allow.append(yaml_str)
        perms["allow"] = allow
        raw["permissions"] = perms
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    except OSError as e:
        logger.warning("持久化永久放行规则失败: %s", e)
