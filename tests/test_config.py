"""config 模块单测。"""

import pytest

from helpcode.config import ConfigError, load


def _write_config(tmp_path, text: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_load_single_provider(tmp_path):
    path = _write_config(
        tmp_path,
        """
providers:
  - name: "claude"
    protocol: anthropic
    api_key: "sk-test"
    model: "claude-sonnet"
""",
    )
    cfg = load(path)
    assert len(cfg.providers) == 1
    p = cfg.providers[0]
    assert p.name == "claude"
    assert p.protocol == "anthropic"
    assert p.model == "claude-sonnet"
    assert p.base_url is None
    assert p.thinking is False


def test_load_multiple_and_optional_fields(tmp_path):
    path = _write_config(
        tmp_path,
        """
providers:
  - name: "c"
    protocol: anthropic
    api_key: "k1"
    model: "m1"
    thinking: true
  - name: "o"
    protocol: openai
    api_key: "k2"
    model: "m2"
    base_url: "https://example.com/v1"
""",
    )
    cfg = load(path)
    assert len(cfg.providers) == 2
    assert cfg.providers[0].thinking is True
    assert cfg.providers[1].base_url == "https://example.com/v1"


def test_missing_api_key(tmp_path):
    path = _write_config(
        tmp_path,
        """
providers:
  - name: "c"
    protocol: anthropic
    model: "m1"
""",
    )
    with pytest.raises(ConfigError, match="api_key"):
        load(path)


def test_invalid_protocol(tmp_path):
    path = _write_config(
        tmp_path,
        """
providers:
  - name: "c"
    protocol: gemini
    api_key: "k"
    model: "m"
""",
    )
    with pytest.raises(ConfigError, match="protocol"):
        load(path)


def test_empty_providers(tmp_path):
    path = _write_config(tmp_path, "providers: []\n")
    with pytest.raises(ConfigError, match="不能为空"):
        load(path)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load(str(tmp_path / "nope.yaml"))
