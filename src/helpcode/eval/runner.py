"""评测编排：一题一个干净工作区、一个全新 Agent，跑完收 patch 再判定。

三个刻意的设计：

1. **每题新建 Agent 与 Conversation**——上下文不能串题，否则第一题的
   中间结果会变成第二题的噪声，分数就没意义了。
2. **自己消费 ``agent.run()`` 而不是 ``run_to_completion()``**——后者只回
   文本，拿不到 token 用量与工具调用次数，而「成本」是评测报告的一半。
3. **串行执行**：先保证可复现，再谈并发。并发会引入限流与计时噪声，
   在样本量只有几十题时得不偿失。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import __version__
from ..agent import Agent
from ..config import ProviderConfig
from ..context import ContextManager
from ..conversation import Conversation
from ..llm import new_provider
from ..permission import Mode, new_engine
from ..tool import new_default_registry
from .driver import Driver
from .instance import Instance, to_row
from .judge import judge
from .patch import PatchError, changed_files, collect_patch

# 评测只给这六个工具：够改代码，又不会让归因变模糊（不要 Agent / Team / Skill）
EVAL_TOOLS = ("read_file", "write_file", "edit_file", "glob", "grep", "bash")

# 通用工作方式提示（不含任何题目解法，符合评测协议）
TASK_WRAPPER = """<task>
{problem}
</task>

工作方式：
- 先用 read_file / grep / glob 看清相关代码，再做最小改动。
- 必须用 edit_file 或 write_file 真正改仓库文件；只在回复里贴代码不算完成。
- 仓库里有测试的话，用 bash 跑一次确认没有再改坏别的东西。"""

# 评测专用系统提示：短、聚焦「必须动手」。
# 用主 Agent 那套长提示时，模型会在「读文件 → 说一句修复如下」之后就停
# （实测 qwen3.7-max 复现两次），而评测要的是一次完整修复。
# SWE-agent / OpenHands 也都是给评测单独一套 prompt——这属于通用工作方式，
# 不含任何具体题目的解法，符合协议。
# 空补丁时的通用追问（不含任何题目解法）。模型偶尔会「只说要做、不发工具调用」，
# 实测 qwen3.7-max 约 1/3 概率；这是 harness 该处理的鲁棒性问题，不是题目难度。
NO_PATCH_NUDGE = (
    "你还没有修改任何文件，任务尚未完成。"
    "请用 edit_file 或 write_file 真正改动仓库文件，然后再结束。"
)

EVAL_SYSTEM = """你是一个自主修复 issue 的工程 agent。你会拿到一段 issue 描述和它对应的代码仓库。

规则：
1. 只依据 issue 描述与仓库现状行动，不要臆测不存在的文件或接口。
2. 必须用工具真实修改文件（edit_file / write_file）。在回复里贴出代码或 diff 不算完成。
3. 改动要小、要准：只动与 issue 相关的文件，不要顺手重构。
4. 仓库里如果有测试，改完用 bash 跑一次，确认没有改坏其它功能。
5. 看到需要改的地方就直接调用 edit_file 修改，不要先输出一段说明再等下个回合。
6. 完成后用一两句话说明改了什么，不要复述过程。

当前工作目录就是仓库根目录，所有路径用相对路径。"""


@dataclass
class InstanceResult:
    """一道题的完整结果。报告、归因、汇总数字都从这里长出来。"""

    instance_id: str
    resolved: bool
    reason: str
    detail: str = ""
    f2p: str = "0/0"
    p2p: str = "0/0"
    turns: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache: int = 0
    elapsed: float = 0.0
    patch_chars: int = 0
    attempts: int = 1
    patch_files: list[str] = field(default_factory=list)
    workspace: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _merge_stats(a: dict, b: dict) -> dict:
    """多次尝试的用量累加；turns 留给调用方按对话统计。"""
    out = dict(a)
    for key in ("tool_calls", "tokens_in", "tokens_out", "tokens_cache"):
        out[key] = int(a.get(key, 0)) + int(b.get(key, 0))
    if b.get("error"):
        out["error"] = b["error"]
    return out


class EvalRunner:
    """串行跑一批题。"""

    def __init__(
        self,
        provider_cfg: ProviderConfig,
        driver: Driver,
        out_dir: Path,
        *,
        max_turns: int = 30,
        timeout: float = 900.0,
        retries: int = 1,
        context_window: int = 128_000,
    ) -> None:
        self.provider_cfg = provider_cfg
        self.driver = driver
        # 必须绝对化：--out 常写成相对路径，而 workspace 既当 git 工作区又当
        # 子进程 cwd，相对路径会在「相对谁」上打架（junit 被写进嵌套目录）。
        self.out_dir = Path(out_dir).resolve()
        self.max_turns = max_turns
        self.timeout = timeout
        self.retries = max(0, retries)
        self.context_window = context_window
        self.patches_dir = self.out_dir / "patches"
        self.traj_dir = self.out_dir / "trajectories"

    def prepare_dirs(self, instances: list[Instance]) -> None:
        self.patches_dir.mkdir(parents=True, exist_ok=True)
        self.traj_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.joinpath("workspaces").mkdir(parents=True, exist_ok=True)
        (self.out_dir / "instances.jsonl").write_text(
            "".join(
                json.dumps(to_row(i), ensure_ascii=False) + "\n" for i in instances
            ),
            encoding="utf-8",
        )

    async def run_all(self, instances: list[Instance]) -> list[InstanceResult]:
        self.prepare_dirs(instances)
        results: list[InstanceResult] = []
        for idx, inst in enumerate(instances, 1):
            print(f"[{idx}/{len(instances)}] {inst.instance_id}", flush=True)
            result = await self.run_one(inst)
            results.append(result)
            flag = "OK " if result.resolved else ".. "
            print(
                f"  {flag}{result.reason} {result.f2p} {result.p2p} "
                f"{result.elapsed:.1f}s {result.tool_calls}工具",
                flush=True,
            )
            self._append_result(result)
        return results

    async def run_one(self, inst: Instance) -> InstanceResult:
        started = time.monotonic()
        workspace = self.out_dir / "workspaces" / inst.instance_id
        workspace.parent.mkdir(parents=True, exist_ok=True)
        patch = ""
        try:
            workspace = self.driver.prepare(inst, self.out_dir / "workspaces")
        except (PatchError, OSError) as e:
            return InstanceResult(
                inst.instance_id,
                False,
                "prepare_failed",
                str(e)[:300],
                elapsed=time.monotonic() - started,
            )

        agent, conv = self._build_agent(workspace)
        task = TASK_WRAPPER.format(problem=inst.problem_statement)
        text, stats = await self._drive(agent, conv, task)
        attempts = 1
        try:
            patch = collect_patch(Path(workspace))
            while not patch.strip() and attempts <= self.retries:
                # 空补丁重试：模型「只说要做、没发工具调用」时给一次通用追问。
                # 追问由 agent.run 作为新的 user 消息追加，不含任何题目信息，
                # 不改变评测难度，只兜住 harness 侧该处理的空转。
                attempts += 1
                text, more = await self._drive(agent, conv, NO_PATCH_NUDGE)
                stats = _merge_stats(stats, more)
                patch = collect_patch(Path(workspace))
        except PatchError as e:
            return InstanceResult(
                inst.instance_id,
                False,
                "patch_failed",
                str(e)[:300],
                elapsed=time.monotonic() - started,
                error=str(e)[:300],
            )

        # 轮次按对话里的 assistant 条数算：Agent 只在最后一轮 yield token_usage，
        # 用它计数永远得到 1（实测踩过），拿不到真实的轮次成本。
        stats["turns"] = sum(1 for m in conv.messages() if m.role == "assistant")

        (self.patches_dir / f"{inst.instance_id}.patch").write_text(
            patch, encoding="utf-8"
        )
        self._dump_trajectory(inst, conv, text)

        outcome = self.driver.run_tests(inst, Path(workspace))
        verdict = judge(inst, outcome, patch)

        return InstanceResult(
            instance_id=inst.instance_id,
            resolved=verdict.resolved,
            reason=verdict.reason,
            detail=verdict.detail,
            f2p=f"{verdict.f2p_passed}/{verdict.f2p_total}",
            p2p=f"{verdict.p2p_passed}/{verdict.p2p_total}",
            turns=stats["turns"],
            tool_calls=stats["tool_calls"],
            tokens_in=stats["tokens_in"],
            tokens_out=stats["tokens_out"],
            tokens_cache=stats["tokens_cache"],
            elapsed=time.monotonic() - started,
            patch_chars=len(patch),
            attempts=attempts,
            patch_files=changed_files(patch),
            workspace=str(workspace),
            error=outcome.error,
        )

    def _build_agent(self, workspace: Path) -> tuple[Agent, Conversation]:
        provider = new_provider(self.provider_cfg)
        registry = new_default_registry().subset(list(EVAL_TOOLS))
        engine, _ = new_engine(str(workspace))
        # 评测默认不开沙箱（开了会连 pytest 的临时文件都要放行），
        # 但状态要接上：将来想验证"沙箱下的 rollout"直接改 enabled 即可。
        registry.attach_sandbox(engine.sandbox)
        context = ContextManager(provider, workspace, self.context_window)
        agent = Agent(
            provider,
            registry,
            __version__,
            engine,
            context,
            system_prompt=EVAL_SYSTEM,
            max_turns=self.max_turns,
            cwd=str(workspace),
        )
        return agent, Conversation()

    async def _drive(
        self, agent: Agent, conv: Conversation, task: str
    ) -> tuple[str, dict]:
        """跑到底，同时统计轮次 / 工具调用 / token。"""

        async def _inner() -> tuple[str, dict]:
            text = ""
            stats = {
                "turns": 0,
                "tool_calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cache": 0,
            }
            cancel = asyncio.Event()
            async for ev in agent.run(task, conv, cancel, Mode.BYPASS):
                if ev.err is not None:
                    stats["error"] = f"{ev.err.__class__.__name__}: {ev.err}"[:300]
                    break
                if ev.text:
                    text += ev.text
                if ev.tool_start is not None:
                    stats["tool_calls"] += 1
                if ev.token_usage is not None:
                    stats["tokens_in"] += ev.token_usage.input_tokens
                    stats["tokens_out"] += ev.token_usage.output_tokens
                    stats["tokens_cache"] += ev.token_usage.cache_read
                if ev.done:
                    break
            return text, stats

        try:
            return await asyncio.wait_for(_inner(), timeout=self.timeout)
        except TimeoutError:
            return "", {
                "turns": 0,
                "tool_calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cache": 0,
                "error": f"超时（{self.timeout:.0f}s）",
            }

    def _dump_trajectory(self, inst: Instance, conv: Conversation, text: str) -> None:
        """轨迹全量落盘——归因和（将来）提交流程都靠它。"""
        lines: list[str] = []
        for msg in conv.messages():
            lines.append(
                json.dumps(
                    {
                        "role": msg.role,
                        "content": msg.content,
                        "tool_call_id": msg.tool_call_id,
                        "tool_calls": [
                            {"id": tc.id, "name": tc.name, "input": tc.input}
                            for tc in (msg.tool_calls or [])
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        lines.append(json.dumps({"role": "final", "content": text}, ensure_ascii=False))
        (self.traj_dir / f"{inst.instance_id}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def _append_result(self, result: InstanceResult) -> None:
        with (self.out_dir / "results.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
