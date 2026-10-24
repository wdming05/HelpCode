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


def _write(tmp_path, providers):
    import yaml

    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump({"providers": providers}, allow_unicode=True), encoding="utf-8"
    )
    return str(p)


def _base(**kw):
    d = {"name": "p", "protocol": "openai", "api_key": "k", "model": "m"}
    d.update(kw)
    return d


def test_context_window_explicit(tmp_path):
    cfg = load(_write(tmp_path, [_base(context_window=64000)]))
    assert cfg.providers[0].context_window == 64000


def test_context_window_default_openai_is_128k(tmp_path):
    cfg = load(_write(tmp_path, [_base()]))
    assert cfg.providers[0].context_window == 128000


def test_context_window_default_anthropic_is_200k(tmp_path):
    cfg = load(_write(tmp_path, [_base(protocol="anthropic")]))
    assert cfg.providers[0].context_window == 200000


def test_context_window_invalid_falls_back(tmp_path):
    for bad in ("abc", 0, -1):
        cfg = load(_write(tmp_path, [_base(context_window=bad)]))
        assert cfg.providers[0].context_window == 128000


def test_context_window_invalid_does_not_raise(tmp_path):
    """可选字段写错不该让整个 CLI 起不来。"""
    cfg = load(_write(tmp_path, [_base(context_window="???")]))
    assert len(cfg.providers) == 1
