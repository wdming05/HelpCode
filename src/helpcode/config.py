"""配置模块：读取并校验 ``.helpcode/config.yaml``。"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

logger = logging.getLogger(__name__)

# 各协议的默认上下文窗口（token）。显式配置 context_window 可覆盖。
# 统一按 128K 会让 Anthropic 用户提前 30% 触发压缩，白白丢掉可用的上下文。
DEFAULT_WINDOW = {"anthropic": 200000, "openai": 128000}


class ConfigError(Exception):
    """配置错误，携带可读信息。"""


@dataclass
class ProviderConfig:
    name: str  # 状态栏左侧显示
    protocol: Literal["anthropic", "openai"]
    api_key: str
    model: str  # 状态栏右侧显示
    base_url: str | None = None  # None 则用 SDK 默认端点
    thinking: bool = False  # 仅 anthropic 生效
    context_window: int = 0  # 0 = 按协议取默认（解析阶段填实）


@dataclass
class Config:
    providers: list[ProviderConfig] = field(default_factory=list)


def load(path: str) -> Config:
    """读取并校验 YAML 配置，返回 Config；失败抛 ConfigError。"""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"配置文件不存在: {path}")

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 解析失败: {e}") from e
    except Exception as e:
        raise ConfigError(f"读取配置文件失败: {e}") from e

    return _from_dict(raw, path)


def _from_dict(raw, path: str) -> Config:
    """把解析后的 dict 映射为 Config，逐项校验。"""
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 mapping")

    raw_providers = raw.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        raise ConfigError("providers 列表不能为空")

    providers = [_parse_provider(item, i) for i, item in enumerate(raw_providers)]
    return Config(providers=providers)


def _parse_provider(item, index: int) -> ProviderConfig:
    """解析单个 provider 条目并校验。"""
    prefix = f"providers[{index}]"
    if not isinstance(item, dict):
        raise ConfigError(f"{prefix} 必须是 mapping")

    name = item.get("name")
    protocol = item.get("protocol")
    api_key = item.get("api_key")
    model = item.get("model")

    # 必要字段非空
    for field_name, value in [
        ("name", name),
        ("protocol", protocol),
        ("api_key", api_key),
        ("model", model),
    ]:
        if not value or not str(value).strip():
            raise ConfigError(f"{prefix}.{field_name} 不能为空")

    protocol = str(protocol).strip()
    if protocol not in ("anthropic", "openai"):
        raise ConfigError(
            f"{prefix}.protocol 必须是 anthropic 或 openai，实际为: {protocol}"
        )

    return ProviderConfig(
        name=str(name).strip(),
        protocol=protocol,  # type: ignore[arg-type]
        api_key=str(api_key).strip(),
        model=str(model).strip(),
        base_url=item.get("base_url") or None,
        thinking=bool(item.get("thinking", False)),
        context_window=_parse_window(item.get("context_window"), protocol, prefix),
    )


def _parse_window(raw, protocol: str, prefix: str) -> int:
    """解析 context_window；非法值回落协议默认，不抛 ConfigError。

    可选字段不该让程序起不来：一个写错的 token 数，代价顶多是「压缩早触发」，
    而抛异常会让整个 CLI 无法启动。
    """
    default = DEFAULT_WINDOW.get(protocol, 128000)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    logger.warning("%s.context_window 非法（%r），已回落为 %d", prefix, raw, default)
    return default
