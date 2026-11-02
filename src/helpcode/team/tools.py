"""团队相关工具：Lead 的调度面 + 队员的协作面。

两类工具不能混：Lead 需要 `TeamCreate` / `TeamDelete`（建队删队），
队员需要任务四件套 + `SendMessage`（干活与通气）。混在一起要么让队员能
删队，要么让 Lead 看不见任务表。
"""

from __future__ import annotations

from ..tool import ToolResult
from .model import TeamError
from .tasks import TaskError


def _err(prefix: str, exc: Exception) -> ToolResult:
    return ToolResult(ok=False, content=f"{prefix}：{exc}", is_error=True)


class TeamCreateTool:
    """建团队。未建队时这是 Lead 唯一的团队入口。"""

    name = "TeamCreate"
    read_only = False
    description = (
        "创建一个协作团队并把当前 Agent 设为 Lead。"
        "创建后会出现共享任务列表与 SendMessage 等协作工具。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "团队名（会规范化为目录名）"},
            "description": {"type": "string", "description": "团队目标的一句话描述"},
        },
        "required": ["name"],
    }

    def __init__(self, service) -> None:
        self._svc = service

    def run(self, name: str, description: str = "") -> ToolResult:
        try:
            team = self._svc.create_team(name, description)
        except Exception as e:
            return _err("创建团队失败", e)
        return ToolResult(
            ok=True,
            content=(
                f"已创建团队「{team.name}」（目录名 {team.sanitized_name}，"
                f"后端 {team.backend}）\n配置：{team.config_path}"
            ),
        )


class TeamDeleteTool:
    name = "TeamDelete"
    read_only = False
    description = "删除团队及其队员工作目录；有活跃成员时会拒绝（可用 force 强制）。"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "团队名，留空删除当前团队"},
            "force": {"type": "boolean", "description": "强制删除（丢弃队员改动）"},
        },
    }

    def __init__(self, service) -> None:
        self._svc = service

    def run(self, name: str = "", force: bool = False) -> ToolResult:
        team = self._svc.active_team
        target = name.strip() or (team.sanitized_name if team else "")
        if not target:
            return ToolResult(ok=False, content="没有可删除的团队", is_error=True)
        try:
            report = self._svc.delete_team(target, force=force)
        except Exception as e:
            return _err("删除团队失败", e)
        removed = "、".join(report["removed_members"]) or "（无队员）"
        lines = [f"已删除团队「{report['team']}」，清理成员：{removed}"]
        lines.extend(f"告警：{w}" for w in report["warnings"])
        return ToolResult(ok=True, content="\n".join(lines))


class TaskCreateTool:
    name = "TaskCreate"
    read_only = False
    description = "在团队共享任务列表里新建一条任务。"
    parameters = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "任务标题"},
            "description": {"type": "string", "description": "任务细节"},
            "owner": {"type": "string", "description": "负责人（成员名），可留空"},
        },
        "required": ["subject"],
    }

    def __init__(self, service, owner: str = "") -> None:
        self._svc = service
        self._owner = owner

    def run(self, subject: str, description: str = "", owner: str = "") -> ToolResult:
        try:
            task = self._svc.tasks().create(subject, description, owner or self._owner)
        except Exception as e:
            return _err("创建任务失败", e)
        return ToolResult(ok=True, content=f"已创建 {task.render()}")


class TaskGetTool:
    name = "TaskGet"
    read_only = True
    description = "查看团队共享任务的详情。"
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "任务 id，如 task-1"}},
        "required": ["id"],
    }

    def __init__(self, service) -> None:
        self._svc = service

    def run(self, id: str) -> ToolResult:
        try:
            task = self._svc.tasks().get(id)
        except Exception as e:
            return _err("读取任务失败", e)
        if task is None:
            return ToolResult(ok=False, content=f"任务不存在：{id}", is_error=True)
        lines = [
            task.render(),
            f"描述：{task.description or '（无）'}",
        ]
        if task.blocks:
            lines.append(f"阻塞：{', '.join(task.blocks)}")
        if task.blocked_by:
            lines.append(f"等待：{', '.join(task.blocked_by)}")
        return ToolResult(ok=True, content="\n".join(lines))


class TaskListTool:
    name = "TaskList"
    read_only = True
    description = "列出团队共享任务（默认不含已删除）。"
    parameters = {
        "type": "object",
        "properties": {
            "include_deleted": {
                "type": "boolean",
                "description": "是否包含已删除任务",
            }
        },
    }

    def __init__(self, service) -> None:
        self._svc = service

    def run(self, include_deleted: bool = False) -> ToolResult:
        try:
            tasks = self._svc.tasks().list(include_deleted=include_deleted)
        except Exception as e:
            return _err("读取任务列表失败", e)
        if not tasks:
            return ToolResult(ok=True, content="（任务列表为空）")
        return ToolResult(ok=True, content="\n".join(t.render() for t in tasks))


class TaskUpdateTool:
    name = "TaskUpdate"
    read_only = False
    description = "更新任务：状态、负责人，以及追加 blocks / blocked_by 依赖。"
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "任务 id"},
            "status": {
                "type": "string",
                "description": "pending / in_progress / completed / deleted",
            },
            "owner": {"type": "string", "description": "负责人（成员名）"},
            "subject": {"type": "string", "description": "新标题"},
            "add_blocks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "追加「我阻塞了谁」",
            },
            "add_blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "追加「我等待谁」",
            },
        },
        "required": ["id"],
    }

    def __init__(self, service, owner: str = "") -> None:
        self._svc = service
        self._owner = owner

    def run(
        self,
        id: str,
        status: str = "",
        owner: str = "",
        subject: str = "",
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
    ) -> ToolResult:
        try:
            task = self._svc.tasks().update(
                id,
                status=status or None,
                owner=owner or None,
                subject=subject or None,
                add_blocks=add_blocks,
                add_blocked_by=add_blocked_by,
            )
        except TaskError as e:
            return ToolResult(ok=False, content=f"更新任务失败：{e}", is_error=True)
        except Exception as e:
            return _err("更新任务失败", e)
        return ToolResult(ok=True, content=f"已更新 {task.render()}")


class SendMessageTool:
    """点对点消息。异步工具：投递到空闲成员时会顺带把他拉起来跑。"""

    name = "SendMessage"
    read_only = False
    description = (
        "给团队里的成员发消息（可发名字、agent id 或 * 广播）。"
        "type 用结构化协议：text / shutdown_request / shutdown_response / "
        "plan_approval_request / plan_approval_response。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "收件人名字、agent id 或 *"},
            "content": {"type": "string", "description": "消息正文"},
            "type": {"type": "string", "description": "消息类型，默认 text"},
            "request_id": {"type": "string", "description": "结构化消息的请求 id"},
            "approve": {
                "type": "boolean",
                "description": "审批结论（仅 plan_approval_response 用）",
            },
            "feedback": {"type": "string", "description": "驳回时的修改建议"},
        },
        "required": ["to", "content"],
    }

    def __init__(self, service, sender: str = "lead") -> None:
        self._svc = service
        self._sender = sender

    async def arun(
        self,
        to: str,
        content: str,
        type: str = "text",
        request_id: str = "",
        approve: bool | None = None,
        feedback: str = "",
    ) -> ToolResult:
        try:
            report = await self._svc.send(
                self._sender,
                to,
                content,
                type=type,
                request_id=request_id,
                approve=approve,
                feedback=feedback,
            )
        except (TeamError, Exception) as e:
            return _err("发送消息失败", e)
        delivered = "、".join(report["delivered"]) or "（无）"
        text = f"已投递给：{delivered}"
        if report["resumed"]:
            text += f"\n已唤醒继续干活：{'、'.join(report['resumed'])}"
        return ToolResult(ok=True, content=text)
