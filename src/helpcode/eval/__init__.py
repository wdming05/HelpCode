"""评测的包入口。

对外只暴露四件事，其余都是内部实现：
``load_instances`` / ``make_driver`` / ``EvalRunner`` / ``write_report``。
"""

from __future__ import annotations

from .dataset import DatasetError, load_instances
from .driver import DockerDriver, LocalDriver, TestOutcome, make_driver
from .instance import Instance
from .judge import Verdict, judge
from .patch import PatchError, changed_files, collect_patch
from .report import summarize, write_report
from .runner import EVAL_TOOLS, EvalRunner, InstanceResult

__all__ = [
    "EVAL_TOOLS",
    "DatasetError",
    "DockerDriver",
    "EvalRunner",
    "Instance",
    "InstanceResult",
    "LocalDriver",
    "PatchError",
    "TestOutcome",
    "Verdict",
    "changed_files",
    "collect_patch",
    "judge",
    "load_instances",
    "make_driver",
    "summarize",
    "write_report",
]
