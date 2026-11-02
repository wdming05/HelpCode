"""TeamService：把「团队」这条链路串起来。

它是唯一同时知道 Agent、Worktree、邮箱、任务表的模块——编排放在一起，
各部件就能各自保持简单：邮箱不懂 Agent，Agent 不懂邮箱。

队员的一次完整生命周期：

    spawn  → 建 worktree、组注册中心、装 inbox → 交给后端启动
    跑完   → finalize 标记空闲 + 通知 Lead（计划模式队员额外发审批请求）
    resume → 从 session 存档恢复对话，用新消息继续
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..conversation import Conversation
from ..memory.restore import restore as restore_session
from ..memory.session import SessionWriter
from ..permission import Mode
from .backend import Backend, SpawnRequest
from .mailbox import Mailbox, Message
from .manager import Manager
from .model import Team, TeamError, TeammateInfo, TeamNotFoundError
from .tasks import TaskStore

logger = logging.getLogger(__name__)


@dataclass
class MemberRuntime:
    """一个队员在**本进程**里的运行态。进程重启后这里为空，靠 session 重建。"""

    member: str
    role: object
    agent: object
    conv: Conversation
    writer: SessionWriter
    mode: Mode
    worktree: str
    task_id: str = ""
    last_result: str = ""


class TeamService:
    """团队编排入口。Lead 的工具与 TUI 命令都调这里。"""

    def __init__(
        self,
        manager: Manager,
        runner,
        *,
        lead_id: str = "lead",
        backend: Backend | None = None,
        mode_getter: Callable[[], Mode] | None = None,
    ) -> None:
        self.manager = manager
        self.runner = runner
        self.lead_id = lead_id
        self._backend_override = backend
        self._mode_getter = mode_getter or (lambda: Mode.DEFAULT)
        self._active: str | None = None
        self._runtime: dict[str, MemberRuntime] = {}
        self._approved: set[str] = set()

    # ------------------------------------------------------------ 基础

    @property
    def active_team(self) -> Team | None:
        if self._active is None:
            return None
        return self.manager.get(self._active)

    def create_team(self, name: str, description: str = "") -> Team:
        # 撞名不当错误：重启后用户最想做的是「回到我的团队」，
        # 所以已存在就直接切换为当前团队（Manager 层仍会给同名加后缀）。
        existing = self.manager.get(name)
        if existing is not None:
            self._active = existing.sanitized_name
            return existing
        team = self.manager.create(
            name,
            description,
            backend=self._backend_override,
            lead_id=self.lead_id,
        )
        self._active = team.sanitized_name
        return team

    def set_active(self, name: str) -> Team:
        team = self.manager.get(name)
        if team is None:
            raise TeamNotFoundError(f"团队不存在：{name}")
        self._active = team.sanitized_name
        return team

    def mailbox(self, team: Team | None = None) -> Mailbox:
        return Mailbox((team or self._require_team()).mailbox_root)

    def tasks(self, team: Team | None = None) -> TaskStore:
        return TaskStore((team or self._require_team()).tasks_path)

    # ------------------------------------------------------------ 派活

    async def spawn(
        self,
        team_name: str,
        role,
        prompt: str,
        *,
        member_name: str = "",
        model: str = "",
        plan_mode_required: bool = False,
    ) -> dict:
        team = self.manager.get(team_name)
        if team is None:
            raise TeamNotFoundError(f"团队不存在：{team_name}")
        member = (member_name or role.name or "member").strip()
        if team.member(member) is not None:
            raise TeamError(f"成员已存在：{member}")

        slug = f"{team.sanitized_name}/{member}"
        # git 子进程会阻塞事件循环，丢线程
        wt = await asyncio.to_thread(self.manager.wt_mgr.create, slug)
        session_dir = team.config_dir / "sessions" / member
        runtime = self._build_runtime(
            team, member, role, str(wt.path), session_dir, model, plan_mode_required
        )
        self._runtime[member] = runtime

        backend = self._backend_override or self.manager.backend_for(team)
        req = SpawnRequest(
            team=team.sanitized_name,
            member=member,
            prompt=prompt,
            worktree=str(wt.path),
            session_dir=str(session_dir),
            model=model,
            agent_type=role.name,
            plan_mode_required=plan_mode_required,
            launcher=self._launcher(team, member, runtime, prompt, plan_mode_required),
        )
        pane_id, agent_id = await backend.spawn(req)

        team.add_member(
            TeammateInfo(
                name=member,
                agent_id=agent_id,
                agent_type="" if role.name == "fork" else role.name,
                model=model,
                worktree_path=str(wt.path),
                branch=wt.branch,
                backend_type=backend.type().value,
                pane_id=pane_id,
                is_active=True,
                plan_mode_required=plan_mode_required,
                session_dir=str(session_dir),
            )
        )
        return {
            "team": team.sanitized_name,
            "member": member,
            "agent_id": agent_id,
            "pane_id": pane_id,
            "worktree": str(wt.path),
            "branch": wt.branch,
            "backend": backend.type().value,
            "plan_mode_required": plan_mode_required,
        }

    # ------------------------------------------------------------ 消息

    async def send(
        self,
        sender: str,
        to: str,
        content: str,
        *,
        type: str = "text",
        request_id: str = "",
        approve: bool | None = None,
        feedback: str = "",
    ) -> dict:
        """投递一条消息；必要时顺带把空闲的收件人拉起来继续干活。"""
        team = self._require_team()
        targets = self._resolve_targets(team, to, sender)
        box = self.mailbox(team)
        for target in targets:
            box.send(
                target,
                Message(
                    sender=sender,
                    content=content,
                    type=type,
                    request_id=request_id,
                    approve=approve,
                    feedback=feedback,
                ),
            )

        resumed: list[str] = []
        if type == "text":
            for target in targets:
                if self._is_idle(team, target):
                    await self.resume(target, content)
                    resumed.append(target)
        elif type == "plan_approval_response" and approve:
            for target in targets:
                self._approved.add(target)
                if self._is_idle(team, target):
                    await self.resume(
                        target,
                        "计划已批准，按方案继续执行。",
                        mode=self._mode_getter(),
                    )
                    resumed.append(target)
        return {"delivered": targets, "resumed": resumed}

    def take_messages(self, member: str) -> list[str]:
        """取未读并渲染成注入文本；没有未读返回空列表。"""
        team = self.active_team
        if team is None:
            return []
        return [m.render() for m in self.mailbox(team).take_unread(member)]

    # ------------------------------------------------------------ 续派

    async def resume(
        self, member: str, content: str, *, mode: Mode | None = None
    ) -> dict:
        team = self._require_team()
        info = team.member(member)
        if info is None:
            raise TeamNotFoundError(f"成员不存在：{member}")
        runtime = self._runtime.get(member) or self._rebuild_runtime(team, info)
        self._runtime[member] = runtime
        if mode is not None:
            runtime.mode = mode

        history = len(runtime.conv.messages())
        team.set_member_active(member, True)
        bt = self.runner.tasks.launch(
            runtime.agent,
            runtime.conv,
            content,
            name=member,
            mode=runtime.mode,
            finalize=self._finalizer(team, member, info.plan_mode_required),
        )
        runtime.task_id = bt.id
        return {"member": member, "task_id": bt.id, "history": history}

    def delete_team(self, name: str, force: bool = False) -> dict:
        """删团队前先关掉本进程持有的会话写入器。

        不关的话，Windows 上文件句柄还开着，`rmtree` 会静默失败，
        留下一个删不掉的配置目录。
        """
        # 先校验再动手：拒绝路径上不该停别人的任务、关别人的写入器
        self.manager.assert_deletable(name, force)
        for member in list(self._runtime):
            runtime = self._runtime.pop(member)
            if runtime.task_id:
                # 先停任务：它跑完会回头写花名册与邮箱，把刚删掉的文件又建出来
                self.runner.tasks.stop(runtime.task_id)
            try:
                runtime.writer.close()
            except Exception:
                logger.warning("关闭成员会话写入器失败：%s", member, exc_info=True)
        report = self.manager.delete(name, force=force)
        if self._active and self.manager.get(self._active) is None:
            self._active = None
        return report

    async def kill(self, member: str) -> bool:
        team = self.active_team
        if team is None:
            return False
        info = team.member(member)
        if info is None:
            return False
        backend = self._backend_override or self.manager.backend_for(team)
        await backend.kill(info.pane_id, info.agent_id)
        runtime = self._runtime.get(member)
        if runtime is not None and runtime.task_id:
            self.runner.tasks.stop(runtime.task_id)
        team.set_member_active(member, False)
        return True

    # ------------------------------------------------------------ 注册中心

    def member_registry(self, role, member: str):
        """队员的工具集：角色过滤后的基础工具 + 协作工具。

        必须**先过滤再注册**协作工具——后台白名单会把它们全部拦掉。
        """
        from ..subagent.tools import filter_registry
        from .tools import (
            SendMessageTool,
            TaskCreateTool,
            TaskGetTool,
            TaskListTool,
            TaskUpdateTool,
        )

        base = self.runner.parent.registry_factory()
        registry = filter_registry(base, role, background=True)
        for tool in (
            TaskCreateTool(self, owner=member),
            TaskGetTool(self),
            TaskListTool(self),
            TaskUpdateTool(self, owner=member),
            SendMessageTool(self, sender=member),
        ):
            registry.register(tool)
        return registry

    def lead_tools(self) -> list:
        """Lead 侧的协作工具（含团队管理）。"""
        from .tools import (
            SendMessageTool,
            TaskCreateTool,
            TaskGetTool,
            TaskListTool,
            TaskUpdateTool,
            TeamDeleteTool,
        )

        return [
            TaskCreateTool(self, owner=self.lead_id),
            TaskGetTool(self),
            TaskListTool(self),
            TaskUpdateTool(self, owner=self.lead_id),
            SendMessageTool(self, sender=self.lead_id),
            TeamDeleteTool(self),
        ]

    # ------------------------------------------------------------ 内部

    def _require_team(self) -> Team:
        team = self.active_team
        if team is None:
            raise TeamError("当前没有活跃团队，请先 /team create <名字>")
        return team

    def _is_idle(self, team: Team, name: str) -> bool:
        info = team.member(name)
        return info is not None and info.is_active is False

    def _resolve_targets(self, team: Team, to: str, sender: str) -> list[str]:
        target = (to or "").strip()
        if target in ("*", "all"):
            return [m.name for m in team.members if m.name != sender]
        by_name = team.member(target)
        if by_name is not None:
            return [by_name.name]
        for info in team.members:
            if info.agent_id == target:
                return [info.name]
        names = "、".join(m.name for m in team.members)
        raise TeamNotFoundError(f"找不到收件人：{to}（可选：{names}）")

    def _build_runtime(
        self,
        team: Team,
        member: str,
        role,
        worktree: str,
        session_dir: Path,
        model: str,
        plan_mode_required: bool,
    ) -> MemberRuntime:
        # can_approve=True：队员在 in-process 后端里跑，主 TUI 就在同一进程，
        # Ask 得能升级上去问人。不给这个参数的话 build 会按「后台一律拒绝」，
        # 队员连一个文件都写不了（实测症状：一直复述「文件写入被拒绝」）。
        agent, base_conv = self.runner.build(
            role, background=True, model_override=model, can_approve=True
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        writer = SessionWriter(session_dir / "conversation.jsonl", model or "")
        conv = Conversation.from_messages(base_conv.messages(), on_append=writer.append)
        agent.cwd = worktree
        agent.registry = self.member_registry(role, member)
        agent.set_inbox(lambda: self.take_messages(member))
        mode = Mode.PLAN if plan_mode_required else self._role_mode(role)
        return MemberRuntime(
            member=member,
            role=role,
            agent=agent,
            conv=conv,
            writer=writer,
            mode=mode,
            worktree=worktree,
        )

    def _rebuild_runtime(self, team: Team, info: TeammateInfo) -> MemberRuntime:
        """进程重启 / 窗格后端回来的成员：从磁盘把对话捞回来。"""
        role = None
        if info.agent_type:
            role = self.runner.catalog.get(info.agent_type)
        if role is None:
            role = self.runner.catalog.fork_base()
        session_dir = (
            Path(info.session_dir)
            if info.session_dir
            else (team.config_dir / "sessions" / info.name)
        )
        runtime = self._build_runtime(
            team,
            info.name,
            role,
            info.worktree_path,
            session_dir,
            info.model,
            info.plan_mode_required,
        )
        try:
            report = restore_session(session_dir / "conversation.jsonl")
        except Exception as e:  # 存档坏了就按新对话继续，不阻塞续派
            logger.warning("恢复成员会话失败，按新对话继续：%s（%s）", info.name, e)
            return runtime
        if report.messages:
            runtime.conv = Conversation.from_messages(
                report.messages, on_append=runtime.writer.append
            )
        return runtime

    def _role_mode(self, role) -> Mode:
        """队员的权限模式：显式声明优先，``default`` 表示「跟随 Lead」。

        为什么要跟随：Lead 已经在 ``/permission bypass`` 下，派出去的队员
        却因为角色写着 ``default`` 而每条写操作都要审批——用户会认为这是 bug
        （实测反馈正是如此）。``default`` 本来就是「没有特别要求」的意思，
        交给当前会话的模式决定最符合直觉。

        ``plan`` / ``dontAsk`` / ``acceptEdits`` / ``bypassPermissions``
        这类显式声明照旧生效，只读角色（explore / plan）的保护不受影响。
        """
        from ..subagent.runner import mode_for

        if role.permission_mode in ("", "default"):
            return self._mode_getter()
        return mode_for(role)

    def _launcher(
        self,
        team: Team,
        member: str,
        runtime: MemberRuntime,
        prompt: str,
        plan_mode_required: bool,
    ):
        async def launch(_req: SpawnRequest) -> tuple[str, Callable | None]:
            bt = self.runner.tasks.launch(
                runtime.agent,
                runtime.conv,
                prompt,
                name=member,
                mode=runtime.mode,
                finalize=self._finalizer(team, member, plan_mode_required),
            )
            runtime.task_id = bt.id
            return bt.id, lambda: self.runner.tasks.stop(bt.id)

        return launch

    def _finalizer(self, team: Team, member: str, plan_mode_required: bool):
        """任务收尾：标记空闲 + 通知 Lead。"""

        def finalize(bt) -> None:
            result = bt.result or ""
            runtime = self._runtime.get(member)
            if runtime is not None:
                runtime.last_result = result
            # 团队已经被删掉了：别再往坟场上写花名册和邮箱
            if self.manager.get(team.sanitized_name) is None:
                return
            try:
                team.set_member_active(member, False)
            except Exception:
                logger.warning("更新成员状态失败：%s", member, exc_info=True)

            box = self.mailbox(team)
            if plan_mode_required and member not in self._approved:
                try:
                    box.send(
                        self.lead_id,
                        Message(
                            sender=member,
                            content=result or "（计划为空）",
                            type="plan_approval_request",
                            request_id=f"plan-{member}",
                            summary=f"{member} 提交计划待审批",
                        ),
                    )
                except Exception:
                    logger.warning("发送计划审批请求失败", exc_info=True)
            try:
                box.send(
                    self.lead_id,
                    Message(
                        sender=member,
                        content=f"{member} 已停下：{(result or '')[:200]}",
                        type="idle_notification",
                        summary=f"{member} 空闲",
                    ),
                )
            except Exception:
                logger.warning("发送空闲通知失败", exc_info=True)

        return finalize
