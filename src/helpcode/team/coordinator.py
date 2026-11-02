"""Coordinator Mode：Lead 只调度、不改代码。

两把锁：
1. 能力开关（配置文件里的 ``features.coordinator_mode``）；
2. 环境变量 ``HELPCODE_COORDINATOR_MODE``。

为什么要两把：只靠环境变量，一次 ``export`` 就会把所有人带进这个模式；
只靠配置，又没法在同一个项目里按会话临时关掉。两把都开才生效，语义最清楚。
"""

from __future__ import annotations

import os

from .backend import env_truthy

# 开启后 Lead 能用的工具：调度 + 读类 + shell（合并分支得靠 shell）
COORDINATOR_TOOLS = frozenset(
    {
        "Agent",
        "TeamCreate",
        "TeamDelete",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskUpdate",
        "SendMessage",
        "read_file",
        "glob",
        "grep",
        "bash",
    }
)

ENV_VAR = "HELPCODE_COORDINATOR_MODE"

_PROMPT = """# 协调者模式（Coordinator Mode）

你现在只负责**调度与决策**，不自己改代码——`write_file` / `edit_file` 已被移除。

按四个阶段推进：

1. **调研**：先用 read_file / glob / grep 看清现状与约束，别猜。
2. **综合**：把目标拆成可并行的子任务，用 TaskCreate 写进共享任务列表（带 blocks / blocked_by）。
3. **实现**：用 Agent(team_name=…) 派队员，每条任务写清「改什么、怎么验证」；队员之间可以直接用 SendMessage 通气，不必全经过你。
4. **验证**：收齐队员结果后，用 bash 跑测试 / 合并分支；冲突自己判断，搞不定就 `git merge --abort` 并如实上报。

约束：
- 你自己不写文件；需要改代码就派队员。
- 派活要给验收标准，不要只说「优化一下」。
- 队员空闲后可以用 SendMessage 继续指派，不必重新 spawn。
"""


def is_coordinator_mode(feature_enabled: bool, env=None) -> bool:
    """双锁判定。``feature_enabled`` 由上层从配置读出来传进来。"""
    env = env if env is not None else os.environ
    return bool(feature_enabled) and env_truthy(env.get(ENV_VAR))


def coordinator_prompt() -> str:
    return _PROMPT
