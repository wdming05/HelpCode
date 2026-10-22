"""Anthropic system 缓存断点的单测（守护回归）。"""

from helpcode.llm import System
from helpcode.llm.anthropic_provider import (
    _to_anthropic_system,
    _to_anthropic_tools,
)


def test_stable_block_has_single_cache_control():
    """稳定块带且仅带一个 cache_control。"""
    blocks = _to_anthropic_system(System(stable="稳定段", environment="环境段"))
    assert len(blocks) == 2
    assert blocks[0]["type"] == "text"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    # 全局仅一个 cache_control
    assert sum(1 for b in blocks if "cache_control" in b) == 1


def test_environment_block_no_cache_control():
    """环境块不带 cache_control。"""
    blocks = _to_anthropic_system(System(stable="稳定段", environment="环境段"))
    env_block = blocks[1]
    assert env_block["text"] == "环境段"
    assert "cache_control" not in env_block


def test_stable_only_single_block():
    """仅稳定段时 system 只有一个块，仍带 cache_control。"""
    blocks = _to_anthropic_system(System(stable="稳定段"))
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_tools_no_cache_control():
    """tools 末尾不带 cache_control。"""
    tools = _to_anthropic_tools(
        [{"name": "read_file", "description": "d", "parameters": {}}]
    )
    assert all("cache_control" not in t for t in tools)
