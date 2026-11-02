"""MCP 配置层测试。覆盖 AC1/AC2/AC3/F1-F3。"""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from helpcode.mcp.config import CONFIG_RELPATH, ServerConfig, load_mcp_config


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _setup(tmp_path: Path, monkeypatch, user: str | None, project: str | None) -> Path:
    home = tmp_path / "home"
    root = tmp_path / "proj"
    home.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    if user is not None:
        _write(home / CONFIG_RELPATH, user)
    if project is not None:
        _write(root / CONFIG_RELPATH, project)
    monkeypatch.setattr(Path, "home", lambda: home)
    return root


def test_two_layer_merge_project_overrides(tmp_path, monkeypatch):
    """同名 server 项目级整条覆盖，不出现字段级混合。"""
    user = """
mcp_servers:
  A:
    type: stdio
    command: python
"""
    project = """
mcp_servers:
  A:
    type: http
    url: http://example.com/mcp
"""
    root = _setup(tmp_path, monkeypatch, user, project)
    cfg = load_mcp_config(root)
    assert cfg["A"].type == "http"
    assert cfg["A"].url == "http://example.com/mcp"
    assert cfg["A"].command == ""  # 用户级的 command 没有被保留


def test_missing_files(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, None, None)
    assert load_mcp_config(root) == {}


def test_invalid_yaml_degraded(tmp_path, monkeypatch, capsys):
    """项目级 YAML 非法 → 跳过该文件，用户级照常加载。"""
    user = """
mcp_servers:
  U:
    type: stdio
    command: python
"""
    root = _setup(tmp_path, monkeypatch, user, "mcp_servers: [1, 2\n")
    cfg = load_mcp_config(root)
    assert "U" in cfg
    err = capsys.readouterr().err
    assert "YAML 解析失败" in err


def test_field_validation_skips_only_bad(tmp_path, monkeypatch, capsys):
    """type 非法 / 缺 command / 缺 url → 只跳过该 server。"""
    project = """
mcp_servers:
  good:
    type: stdio
    command: python
  no_command:
    type: stdio
  no_url:
    type: http
  bad_type:
    type: socket
    command: python
"""
    root = _setup(tmp_path, monkeypatch, None, project)
    cfg = load_mcp_config(root)
    assert list(cfg) == ["good"]
    err = capsys.readouterr().err
    assert "no_command" in err and "no_url" in err and "bad_type" in err


def test_env_and_headers_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN", "abc123")
    project = """
mcp_servers:
  s:
    type: http
    url: http://x/mcp
    headers:
      Authorization: "Bearer ${TOKEN}"
"""
    root = _setup(tmp_path, monkeypatch, None, project)
    cfg = load_mcp_config(root)
    assert cfg["s"].headers["Authorization"] == "Bearer abc123"


def test_undefined_var_becomes_empty_with_dedup(tmp_path, monkeypatch, capsys):
    """未定义变量 → 空串；同一 server 同一变量只告警一次。"""
    project = """
mcp_servers:
  s:
    type: stdio
    command: python
    env:
      A: "${NOPE_VAR}"
      B: "pre-${NOPE_VAR}"
      C: "${NOPE_VAR}-post"
"""
    root = _setup(tmp_path, monkeypatch, None, project)
    cfg = load_mcp_config(root)
    assert cfg["s"].env["A"] == ""
    assert cfg["s"].env["B"] == "pre-"
    err = capsys.readouterr().err
    assert err.count("NOPE_VAR") == 1


def test_command_args_not_expanded(tmp_path, monkeypatch):
    """command 不做变量展开（避免命令被环境间接影响）。"""
    project = """
mcp_servers:
  s:
    type: stdio
    command: "${HOME}/bin/x"
    args:
      - "${ALSO_NOT}"
"""
    root = _setup(tmp_path, monkeypatch, None, project)
    cfg = load_mcp_config(root)
    assert cfg["s"].command == "${HOME}/bin/x"
    assert cfg["s"].args == ("${ALSO_NOT}",)


def test_output_sorted_and_frozen(tmp_path, monkeypatch):
    project = """
mcp_servers:
  zeta:
    type: stdio
    command: python
  alpha:
    type: stdio
    command: python
"""
    root = _setup(tmp_path, monkeypatch, None, project)
    cfg = load_mcp_config(root)
    assert list(cfg) == ["alpha", "zeta"]
    with pytest.raises(FrozenInstanceError):
        cfg["alpha"].command = "x"  # frozen
    assert isinstance(cfg["alpha"], ServerConfig)
