"""题目来源：本地 JSONL / YAML 或 HuggingFace 数据集。

为什么不强依赖 ``datasets``：这台机器上它没装，而评测链路的核心是
「跑题 → 收 patch → 判定」，不该被一个数据加载库卡住。装了就用，
没装就只支持本地 JSONL，并给一句明确的安装提示。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from .instance import Instance, from_row

HF_PREFIX = "hf:"


class DatasetError(RuntimeError):
    """题集加载失败（文件缺失、字段不合法、HF 依赖缺失等）。"""


def load_instances(
    source: str,
    *,
    split: str = "lite",
    limit: int | None = None,
    instance_ids: Iterable[str] | None = None,
) -> list[Instance]:
    """按来源加载题目，并按 ``limit`` / ``instance_ids`` 过滤。

    ``source`` 三种写法：
    - ``path/to/instances.jsonl``：一行一题
    - ``path/to/instances.json``：一个数组
    - ``hf:SWE-bench-Live/SWE-bench-Live``：走 HuggingFace（需要 datasets）
    """
    wanted = {str(x) for x in (instance_ids or []) if str(x)}
    items = (
        _load_hf(source, split) if source.startswith(HF_PREFIX) else _load_file(source)
    )

    if wanted:
        items = [x for x in items if x.instance_id in wanted]
    if limit is not None and limit > 0:
        items = items[:limit]
    if not items:
        raise DatasetError(f"没有可用题目（source={source}, split={split}）")
    return items


def _load_file(source: str) -> list[Instance]:
    path = Path(source)
    if not path.exists():
        raise DatasetError(f"题集文件不存在：{source}")

    text = path.read_text(encoding="utf-8")
    base_dir = str(path.parent)
    try:
        if path.suffix.lower() == ".json":
            rows = json.loads(text)
            if isinstance(rows, dict):
                rows = rows.get("instances") or list(rows.values())
        else:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError as e:
        raise DatasetError(f"题集解析失败（{source}）：{e}") from e

    if not isinstance(rows, list):
        raise DatasetError(f"题集格式应为数组或 JSONL：{source}")
    try:
        return [from_row(r, base_dir=base_dir) for r in rows if isinstance(r, dict)]
    except ValueError as e:
        raise DatasetError(f"题集字段不合法：{e}") from e


def _load_hf(source: str, split: str) -> list[Instance]:
    name = source[len(HF_PREFIX) :]
    try:
        from datasets import load_dataset
    except ImportError as e:  # pragma: no cover - 取决于环境
        raise DatasetError(
            "读取 HuggingFace 数据集需要 datasets 库：pip install datasets\n"
            "或者先用官方脚本导出 JSONL，再用本地文件模式。"
        ) from e

    try:
        ds = load_dataset(name, split=split)
    except Exception as e:
        raise DatasetError(f"加载 HF 数据集失败（{name} / {split}）：{e}") from e

    return [from_row(dict(row)) for row in ds]
