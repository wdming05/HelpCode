"""评测实例的数据模型。

字段对齐 SWE-bench / SWE-bench-Live 的 HF 数据集，但**不依赖 datasets 库**——
离线环境（或没装 datasets 的机器）也要能读本地 JSONL 跑评测。

一个实例 = 一道题：
- ``problem_statement`` 是给 agent 看的全部信息（协议规定只能给这个）
- ``patch`` / ``test_patch`` / ``fail_to_pass`` / ``pass_to_pass`` 只给评测器，
  **绝不能出现在 agent 的上下文里**
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Instance:
    """一道评测题。"""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    # 下面四项属于「评测侧机密」：agent 可见范围之外
    patch: str = ""
    test_patch: str = ""
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    test_cmds: list[str] = field(default_factory=list)
    image_key: str = ""
    log_parser: str = "pytest"
    # 本地合成题用：题目仓库的相对路径（相对数据集文件所在目录）
    local_repo: str = ""
    # 「无需修改」题：代码本来就对，agent 不该动。patch 为空 + 测试全过才算 resolved
    no_change: bool = False

    def __post_init__(self) -> None:
        self.instance_id = str(self.instance_id).strip()
        if not self.instance_id:
            raise ValueError("instance_id 不能为空")

    @property
    def is_local(self) -> bool:
        """本地合成题：仓库就在磁盘上，不需要 clone，也不需要镜像。"""
        return bool(self.local_repo)

    def public_view(self) -> dict[str, str]:
        """给 agent 的部分。只含 problem_statement —— 这是协议要求。"""
        return {
            "instance_id": self.instance_id,
            "problem_statement": self.problem_statement,
        }


def _as_list(value: Any) -> list[str]:
    """HF 里 FAIL_TO_PASS 可能是 list，也可能是 JSON 字符串。"""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return [text]
            return [str(x) for x in parsed] if isinstance(parsed, list) else [text]
        return [text]
    return [str(value)]


def from_row(row: dict[str, Any], *, base_dir: str = "") -> Instance:
    """把一行原始记录（HF row 或 JSONL 字典）变成 Instance。

    ``base_dir`` 用于解析 ``local_repo`` 的相对路径——本地题集跟着数据集文件走，
    换机器不用改路径。
    """
    local_repo = str(row.get("local_repo") or "")
    if local_repo and base_dir:
        from pathlib import Path

        local_repo = str((Path(base_dir) / local_repo).resolve())

    return Instance(
        instance_id=str(row.get("instance_id") or ""),
        repo=str(row.get("repo") or ""),
        base_commit=str(row.get("base_commit") or ""),
        problem_statement=str(row.get("problem_statement") or ""),
        patch=str(row.get("patch") or ""),
        test_patch=str(row.get("test_patch") or ""),
        fail_to_pass=_as_list(row.get("FAIL_TO_PASS")),
        pass_to_pass=_as_list(row.get("PASS_TO_PASS")),
        test_cmds=_as_list(row.get("test_cmds")),
        image_key=str(row.get("image_key") or ""),
        log_parser=str(row.get("log_parser") or "pytest"),
        local_repo=local_repo,
        no_change=bool(row.get("no_change")),
    )


def to_row(inst: Instance) -> dict[str, Any]:
    """序列化回 JSONL（写 instances.jsonl 用）。"""
    return {
        "instance_id": inst.instance_id,
        "repo": inst.repo,
        "base_commit": inst.base_commit,
        "problem_statement": inst.problem_statement,
        "patch": inst.patch,
        "test_patch": inst.test_patch,
        "FAIL_TO_PASS": inst.fail_to_pass,
        "PASS_TO_PASS": inst.pass_to_pass,
        "test_cmds": inst.test_cmds,
        "image_key": inst.image_key,
        "log_parser": inst.log_parser,
        "local_repo": inst.local_repo,
        "no_change": inst.no_change,
    }
