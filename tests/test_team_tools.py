"""团队工具：七个工具的行为、可见性、寻址、Agent 工具分流。"""

import asyncio

import pytest
from team_env import build, role

from helpcode.config import ProviderConfig
from helpcode.subagent.tools import AgentTool
from helpcode.team.model import TeammateInfo
from helpcode.team.tools import (
    SendMessageTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    TeamCreateTool,
    TeamDeleteTool,
)
from helpcode.tui.app import HelpCodeApp

_CFG = [ProviderConfig(name="qwen", protocol="openai", api_key="x", model="m")]


@pytest.fixture
def env(tmp_path):
    return build(tmp_path)


def test_all_tools_run(env):
    created = TeamCreateTool(env.svc).run("alpha", "第二团队")
    assert created.ok and "alpha" in created.content

    made = TaskCreateTool(env.svc, owner="lead").run("拆模块", "先拆配置")
    assert made.ok and "task-1" in made.content

    got = TaskGetTool(env.svc).run("task-1")
    assert got.ok and "拆模块" in got.content

    listed = TaskListTool(env.svc).run()
    assert listed.ok and "task-1" in listed.content

    updated = TaskUpdateTool(env.svc, owner="lead").run(
        "task-1", status="completed", add_blocks=["task-2"]
    )
    assert updated.ok and "completed" in updated.content

    env.svc.active_team.add_member(TeammateInfo(name="alice", agent_id="agent-1"))
    sent = asyncio.run(SendMessageTool(env.svc, "lead").arun("alice", "开工"))
    assert sent.ok and "alice" in sent.content

    deleted = TeamDeleteTool(env.svc).run("alpha", force=True)
    assert deleted.ok and "alpha" in deleted.content


def test_tool_errors_are_structured(env):
    bad_task = TaskGetTool(env.svc).run("task-404")
    assert bad_task.is_error
    bad_send = asyncio.run(SendMessageTool(env.svc, "lead").arun("nobody", "hi"))
    assert bad_send.is_error and "找不到收件人" in bad_send.content
    bad_delete = TeamDeleteTool(env.svc).run("nope")
    assert bad_delete.is_error


def test_send_addressing(env):
    env.team.add_member(TeammateInfo(name="alice", agent_id="agent-1"))
    env.team.add_member(TeammateInfo(name="bob", agent_id="agent-2"))
    tool = SendMessageTool(env.svc, "lead")

    by_name = asyncio.run(tool.arun("alice", "按名字"))
    assert by_name.ok and "alice" in by_name.content

    by_id = asyncio.run(tool.arun("agent-2", "按 id"))
    assert by_id.ok and "bob" in by_id.content

    broadcast = asyncio.run(tool.arun("*", "全体"))
    assert broadcast.ok and "alice" in broadcast.content and "bob" in broadcast.content

    box = env.svc.mailbox(env.team)
    assert box.unread_count("alice") == 2
    assert box.unread_count("bob") == 2


def test_agent_tool_routes_to_team(env):
    tool = AgentTool(env.runner, env.svc)

    async def scenario():
        result = await tool.arun(
            "写点东西",
            description="alice",
            subagent_type="explore",
            team_name=env.team.sanitized_name,
        )
        await env.tasks.drain()
        return result

    result = asyncio.run(scenario())
    assert result.ok, result.content
    assert "已派出队员 alice" in result.content
    member = env.team.member("alice")
    assert member is not None and member.agent_type == "explore"


def test_agent_tool_without_service(env):
    tool = AgentTool(env.runner, None)
    result = asyncio.run(
        tool.arun("x", description="y", subagent_type="explore", team_name="refactor")
    )
    assert result.is_error and "Team 未启用" in result.content


def test_agent_tool_unknown_team_member_role(env):
    tool = AgentTool(env.runner, env.svc)
    result = asyncio.run(
        tool.arun("x", description="y", subagent_type="nope", team_name="refactor")
    )
    assert result.is_error and "未知 subagent_type" in result.content


def test_visibility(env):
    """未建队只有 TeamCreate；建队后有协作工具；队员侧没有 Agent/TeamCreate。"""
    app = HelpCodeApp(_CFG, teams=env.mgr)
    app.team_service = env.svc
    app.subagent_runner = env.runner

    # 先造一个「还没建队」的服务视图
    app.team_service = type(
        "S",
        (),
        {
            "active_team": None,
            "create_team": env.svc.create_team,
            "lead_tools": env.svc.lead_tools,
        },
    )()
    names = set(app._build_registry().names())
    assert "TeamCreate" in names
    assert "TaskCreate" not in names
    assert "SendMessage" not in names

    app.team_service = env.svc
    names = set(app._build_registry().names())
    assert {"TeamCreate", "TeamDelete", "TaskCreate", "SendMessage"} <= names

    member_names = set(env.svc.member_registry(role(env), "alice").names())
    assert "TeamCreate" not in member_names and "Agent" not in member_names
