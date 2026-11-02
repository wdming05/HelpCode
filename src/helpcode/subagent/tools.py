"""子 Agent 相关的四个工具，以及工具过滤多层防线。"""

from __future__ import annotations

import os

from ..tool import ToolResult
from .catalog import Role

# 全局禁止：任何子 Agent 都看不到 Agent 工具（F3/F17）
GLOBAL_DISALLOWED = frozenset({"Agent"})

# 后台子 Agent 只能用基础读写/搜索/执行 + load_skill（F17）
BACKGROUND_ALLOWED = frozenset(
    {"read_file", "write_file", "edit_file", "bash", "glob", "grep", "load_skill"}
)


def filter_registry(base, role: Role, background: bool):
    """按「全局禁止 → 角色白/黑名单 → 后台白名单」过滤工具池。"""
    names = set(base.names())
    names -= GLOBAL_DISALLOWED
    if role.tools:
        names &= set(role.tools)
    names -= set(role.disallowed_tools)
    if background:
        names &= BACKGROUND_ALLOWED
    return base.subset(names)


class AgentTool:
    """主 Agent 调用子 Agent 的统一入口。"""

    name = "Agent"
    read_only = False
    description = (
        "把一个子任务委派给独立的子 Agent，它有自己的上下文与受限工具集。"
        "subagent_type 指定预定义角色；留空则 Fork 当前对话。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "交给子 Agent 的任务指令"},
            "description": {"type": "string", "description": "一句话描述任务"},
            "subagent_type": {
                "type": "string",
                "description": "预定义角色名；留空走 Fork（继承当前对话）",
            },
            "model": {
                "type": "string",
                "description": "模型覆盖：inherit 或具体模型名",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "true 时后台执行；Fork 强制后台",
            },
            "name": {"type": "string", "description": "给本次子 Agent 命名"},
            "team_name": {
                "type": "string",
                "description": "团队名：非空时作为 Team 队员派出（需要先建队）",
            },
            "plan_mode_required": {
                "type": "boolean",
                "description": "作为队员时是否要求先提交计划等 Lead 批准",
            },
        },
        "required": ["prompt", "description"],
    }

    def __init__(self, runner, team_service=None) -> None:
        self._runner = runner
        self._team = team_service

    async def arun(
        self,
        prompt: str,
        description: str = "",
        subagent_type: str = "",
        model: str = "",
        run_in_background: bool = False,
        name: str = "",
        team_name: str = "",
        plan_mode_required: bool = False,
    ) -> ToolResult:
        catalog = self._runner.catalog

        # ch15：指定团队时走 Team 分支——建 worktree、注入协作工具、登记花名册
        if team_name and team_name.strip():
            if self._team is None:
                return ToolResult(
                    ok=False,
                    content="Team 未启用（当前会话没有团队服务）",
                    is_error=True,
                )
            role = self._team_role(subagent_type)
            if role is None:
                available = "、".join(r.name for r in catalog.all())
                return ToolResult(
                    ok=False,
                    content=f"未知 subagent_type：{subagent_type}。可用：{available}",
                    is_error=True,
                )
            try:
                report = await self._team.spawn(
                    team_name,
                    role,
                    prompt,
                    member_name=name or description,
                    model=model,
                    plan_mode_required=bool(plan_mode_required),
                )
            except Exception as e:
                return ToolResult(ok=False, content=f"派出队员失败：{e}", is_error=True)
            return ToolResult(
                ok=True,
                content=(
                    f"已派出队员 {report['member']}（agent_id={report['agent_id']}，"
                    f"后端 {report['backend']}）\n"
                    f"工作目录：{report['worktree']}\n分支：{report['branch']}"
                    + (
                        "\n该队员需先提交计划，等 Lead 用 SendMessage 审批。"
                        if report["plan_mode_required"]
                        else ""
                    )
                ),
            )

        role = None
        if subagent_type and subagent_type.strip():
            role = catalog.get(subagent_type)
            if role is None:
                available = "、".join(r.name for r in catalog.all())
                return ToolResult(
                    ok=False,
                    content=f"未知 subagent_type：{subagent_type}。可用：{available}",
                    is_error=True,
                )
        else:
            role = catalog.fork_base()

        result = await self._runner.run(
            role,
            prompt,
            background=bool(run_in_background),
            name=name or description,
            model_override=model,
        )
        if result["status"] == "completed":
            return ToolResult(ok=True, content=result.get("result", ""))
        if result["status"] == "failed":
            # ch14：隔离创建失败等前置错误——明确告诉模型，别伪装成「已完成」
            return ToolResult(
                ok=False,
                content=result.get("error", "子 Agent 启动失败"),
                is_error=True,
            )
        return ToolResult(
            ok=True,
            content=f"[{result['status']}] task_id={result.get('task_id', '')}",
        )

    def _team_role(self, subagent_type: str):
        """队员角色：指定了就用指定，留空按 Fork 开关决定。"""
        catalog = self._runner.catalog
        if subagent_type.strip():
            return catalog.get(subagent_type)
        # Fork 队员会把 Lead 的完整对话带过去，成本高，默认关闭
        if (os.environ.get("HELPCODE_TEAM_FORK") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return catalog.fork_base()
        return catalog.get("general-purpose")


class TaskListTool:
    name = "TaskList"
    read_only = True
    description = "列出所有后台子 Agent 任务及其状态。"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, manager) -> None:
        self._manager = manager

    def run(self) -> ToolResult:
        tasks = self._manager.list()
        if not tasks:
            return ToolResult(ok=True, content="（无后台任务）")
        lines = [
            f"- {t.id} [{t.status}] {t.name or '-'} {t.result[:50]}".rstrip()
            for t in tasks
        ]
        return ToolResult(ok=True, content="\n".join(lines))


class TaskGetTool:
    name = "TaskGet"
    read_only = True
    description = "查看某个后台任务的详情与结果。"
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "任务 id"}},
        "required": ["id"],
    }

    def __init__(self, manager) -> None:
        self._manager = manager

    def run(self, id: str) -> ToolResult:
        task = self._manager.get(id)
        if task is None:
            return ToolResult(ok=False, content=f"未找到任务：{id}", is_error=True)
        detail = (
            f"id={task.id}\nname={task.name}\nstatus={task.status}\n"
            f"result={task.result}\nerror={task.error}"
        )
        return ToolResult(ok=True, content=detail)


class TaskStopTool:
    name = "TaskStop"
    read_only = False
    description = "停止一个正在运行的后台任务。"
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "任务 id"}},
        "required": ["id"],
    }

    def __init__(self, manager) -> None:
        self._manager = manager

    def run(self, id: str) -> ToolResult:
        ok = self._manager.stop(id)
        return ToolResult(
            ok=ok,
            content=("已停止 " if ok else "无法停止（不存在或已结束） ") + id,
            is_error=not ok,
        )
