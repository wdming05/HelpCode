"""子 Agent 构造与执行：定义式 / Fork、前台等待、后台托管。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace

from ..agent import Agent
from ..conversation import Conversation
from ..llm import Provider, new_provider
from ..permission import Mode, Outcome
from ..worktree.notice import build_worktree_notice
from .builtin import FORK_BOILERPLATE
from .catalog import Catalog, Role
from .task import TaskManager
from .tools import filter_registry

logger = logging.getLogger(__name__)

MODE_MAP = {
    "default": Mode.DEFAULT,
    "acceptEdits": Mode.ACCEPT_EDITS,
    "plan": Mode.PLAN,
    "bypassPermissions": Mode.BYPASS,
    "dontAsk": Mode.DEFAULT,
}

DEFAULT_AUTO_BACKGROUND = 120.0


def mode_for(role: Role) -> Mode:
    return MODE_MAP.get(role.permission_mode, Mode.DEFAULT)


async def _deny(call, reason) -> Outcome:
    """后台子 Agent 无人可问时的兜底：拒绝，不阻塞。"""
    return Outcome.DENY_ONCE


@dataclass
class ParentContext:
    """父 Agent 侧能力的打包，供 runner 构造子 Agent。"""

    provider: Provider
    provider_cfg: object | None
    engine: object
    hooks: object | None
    version: str
    registry_factory: Callable
    context_factory: Callable
    conv_getter: Callable
    approval_handler: Callable | None = None


class SubAgentRunner:
    """把角色 + 任务变成一个可运行的子 Agent。"""

    def __init__(
        self,
        catalog: Catalog,
        tasks: TaskManager,
        parent: ParentContext,
        auto_background: float = DEFAULT_AUTO_BACKGROUND,
        worktrees=None,
    ) -> None:
        self.catalog = catalog
        self.tasks = tasks
        self.parent = parent
        self.auto_background = auto_background
        # ch14：Worktree 管理器（鸭子类型）。None = 隔离能力不可用，
        # 声明了 isolation 的角色会退化成共享目录（并在结果里说明）。
        self.worktrees = worktrees

    def _provider_for(self, model_override: str) -> Provider:
        cfg = self.parent.provider_cfg
        if model_override and model_override != "inherit" and cfg is not None:
            return new_provider(replace(cfg, model=model_override))
        return self.parent.provider

    def build(
        self,
        role: Role,
        *,
        background: bool,
        model_override: str = "",
        can_approve: bool | None = None,
    ) -> tuple[Agent, Conversation]:
        """构造子 Agent 与它的对话。

        Fork 继承父对话历史（深拷贝）；定义式从空白对话开始。

        ``background`` 与 ``can_approve`` 是两件事，别混：

        - ``background`` 说的是「不阻塞等待」（执行编排）；
        - ``can_approve`` 说的是「有没有人可问」（权限升级）。

        早先把两者绑成一个（后台一律 ``_deny``），结果 Team 队员的写操作
        被永久拒绝——它跑在同一个进程里、主 TUI 就在旁边，明明问得到人。
        ``can_approve`` 缺省仍等于 ``not background``，保持 ch13 语义不变。
        """
        provider = self._provider_for(model_override)
        registry = filter_registry(self.parent.registry_factory(), role, background)
        if role.name == "fork":
            conv = Conversation.from_messages(self.parent.conv_getter().messages())
        else:
            conv = Conversation()

        approve_ok = (not background) if can_approve is None else bool(can_approve)
        approval = self.parent.approval_handler if approve_ok else _deny

        agent = Agent(
            provider,
            registry,
            self.parent.version,
            self.parent.engine,
            self.parent.context_factory(),
            system_prompt=role.body,
            max_turns=role.max_turns,
            auto_approve_ask=(role.permission_mode == "dontAsk"),
            approval_handler=approval,
            hooks=self.parent.hooks,
            skills=None,
        )
        return agent, conv

    async def run(
        self,
        role: Role,
        prompt: str,
        *,
        background: bool,
        name: str = "",
        model_override: str = "",
    ) -> dict:
        """执行一次子任务；返回 completed / async_launched / timed_out_to_background。"""
        if role.isolation == "worktree" and self.worktrees is not None:
            return await self._run_isolated(
                role,
                prompt,
                background=background,
                name=name,
                model_override=model_override,
            )

        is_fork = role.name == "fork"
        run_bg = bool(background or is_fork or role.background)
        agent, conv = self.build(role, background=run_bg, model_override=model_override)
        task_text = prompt if not is_fork else f"{FORK_BOILERPLATE}\n\n{prompt}"
        bt = self.tasks.launch(agent, conv, task_text, name=name, mode=mode_for(role))

        if run_bg:
            return {"status": "async_launched", "task_id": bt.id}

        try:
            # shield：超时只停止等待，后台任务继续跑
            await asyncio.wait_for(
                asyncio.shield(bt.future), timeout=self.auto_background
            )
        except TimeoutError:
            return {"status": "timed_out_to_background", "task_id": bt.id}
        return {"status": "completed", "task_id": bt.id, "result": bt.result}

    async def _run_isolated(
        self,
        role: Role,
        prompt: str,
        *,
        background: bool,
        name: str,
        model_override: str,
    ) -> dict:
        """在独立 Worktree 里跑：创建 → 注入路径说明 → 设 cwd → 跑完清理。"""
        wt_name = "agent-a" + secrets.token_hex(4)[:7]
        try:
            # git 子进程是阻塞的，丢线程里跑，别卡住事件循环
            wt = await asyncio.to_thread(self.worktrees.create, wt_name, "HEAD")
        except Exception as e:
            # 不静默降级成共享目录：那样子 Agent 会直接在主工作区乱写
            return {"status": "failed", "error": f"创建 Worktree 失败：{e}"}

        run_bg = bool(background or role.background)
        agent, conv = self.build(role, background=run_bg, model_override=model_override)
        agent.cwd = str(wt.path)  # 决定所有文件操作落在哪
        notice = build_worktree_notice(str(self.worktrees.repo_root), str(wt.path))
        task_text = f"{notice}\n\n{prompt}"

        manager = self.worktrees

        def finalize(bt) -> None:
            """结果写入后、通知发出前：无变更删掉，有变更保留并补一句路径。"""
            try:
                report = manager.auto_cleanup(wt_name)
            except Exception:
                logger.warning("Worktree 清理失败（已忽略）", exc_info=True)
                return
            if report.kept:
                extra = f"\n\n[Worktree 保留在 {report.path}，分支 {report.branch}]"
                bt.result = f"{bt.result}{extra}" if bt.result else extra.strip()

        bt = self.tasks.launch(
            agent,
            conv,
            task_text,
            name=name or wt_name,
            mode=mode_for(role),
            finalize=finalize,
        )

        if run_bg:
            return {"status": "async_launched", "task_id": bt.id}
        try:
            await asyncio.wait_for(
                asyncio.shield(bt.future), timeout=self.auto_background
            )
        except TimeoutError:
            return {"status": "timed_out_to_background", "task_id": bt.id}
        return {"status": "completed", "task_id": bt.id, "result": bt.result}
