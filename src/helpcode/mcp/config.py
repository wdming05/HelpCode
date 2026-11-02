"""MCP 配置加载：两层 YAML 合并 + 字段校验 + ``${VAR}`` 展开。

与 ``helpcode.config`` 刻意解耦：后者要求 ``providers`` 非空（用户级配置常常只有
``mcp_servers``），硬复用会让「没有 provider 就没有 MCP」这种错误耦合出现。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

# 用户级与项目级用同一个相对路径，只是根不同
CONFIG_RELPATH = Path(".helpcode") / "config.yaml"

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class ServerConfig:
    """一个 MCP server 的配置（已校验、已展开变量）。"""

    name: str
    type: Literal["stdio", "http"]
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def _warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def load_mcp_config(root: Path) -> dict[str, ServerConfig]:
    """读取用户级 + 项目级配置并按 server 名合并；项目级整条覆盖用户级。

    任何文件层面的问题都只降级该文件为空，绝不抛异常。
    """
    user = _read_section(Path.home() / CONFIG_RELPATH)
    project = _read_section(Path(root) / CONFIG_RELPATH)
    merged: dict[str, Any] = {**user, **project}  # 整条覆盖，不做字段级合并

    warned: set[tuple[str, str]] = set()
    result: dict[str, ServerConfig] = {}
    for name in sorted(merged):
        cfg = _parse_server(name, merged[name], warned)
        if cfg is not None:
            result[name] = cfg
    return result


def _read_section(path: Path) -> dict[str, Any]:
    """读一个配置文件的 ``mcp_servers`` 段；任何异常都降级为空 dict。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        _warn(f"MCP 配置读取失败，已跳过: {path} ({e})")
        return {}

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        _warn(f"MCP 配置 YAML 解析失败，已跳过: {path} ({e})")
        return {}

    if not isinstance(raw, dict):
        if raw is not None:
            _warn(f"MCP 配置顶层不是 mapping，已跳过: {path}")
        return {}

    section = raw.get("mcp_servers")
    if not isinstance(section, dict):
        if section is not None:
            _warn(f"MCP 配置的 mcp_servers 不是 mapping，已跳过: {path}")
        return {}
    return section


def _parse_server(
    name: str, raw: Any, warned: set[tuple[str, str]]
) -> ServerConfig | None:
    """校验单个 server 定义；不合法返回 None（调用方跳过并继续）。"""
    if not isinstance(raw, dict):
        _warn(f"MCP server {name!r} 的定义不是 mapping，已跳过")
        return None

    stype = raw.get("type")
    if stype not in ("stdio", "http"):
        _warn(f"MCP server {name!r} 的 type 非法或缺失（须为 stdio / http），已跳过")
        return None

    if stype == "stdio":
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            _warn(f"MCP server {name!r} 缺少必填字段 command，已跳过")
            return None
        return ServerConfig(
            name=name,
            type="stdio",
            command=command.strip(),
            args=_str_tuple(raw.get("args")),
            env=_expand_env(_str_map(raw.get("env")), name, warned),
        )

    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        _warn(f"MCP server {name!r} 缺少必填字段 url，已跳过")
        return None
    return ServerConfig(
        name=name,
        type="http",
        url=url.strip(),
        headers=_expand_env(_str_map(raw.get("headers")), name, warned),
    )


def _str_tuple(value: Any) -> tuple[str, ...]:
    """把 args 容错成字符串元组：非 list 视为空，元素非 str 丢弃。"""
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str))


def _str_map(value: Any) -> dict[str, str]:
    """把 env / headers 容错成字符串映射：非 dict 视为空，值统一转 str。"""
    if not isinstance(value, dict):
        return {}
    return {
        str(k): str(v) for k, v in value.items() if isinstance(v, str | int | float)
    }


def _expand_env(
    values: dict[str, str], server: str, warned: set[tuple[str, str]]
) -> dict[str, str]:
    """展开 ``${VAR}``；未定义变量展开为空串，同一 (server, var) 只告警一次。"""

    def replace(match: re.Match[str]) -> str:
        var = match.group(1)
        found = os.environ.get(var)
        if found is not None:
            return found
        if (server, var) not in warned:
            warned.add((server, var))
            _warn(
                f"MCP server {server!r} 引用了未定义环境变量 ${{{var}}}，"
                f"已展开为空串（server 可能因此鉴权失败）"
            )
        return ""

    return {k: _VAR_RE.sub(replace, v) for k, v in values.items()}
