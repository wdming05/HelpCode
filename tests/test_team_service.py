"""TeamService：派活、空闲通知、续派、计划审批、删队。"""

import asyncio
from pathlib import Path

import pytest
from git_repo import git, make_repo
from null_context import NullContext

from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent, ToolCall
from helpcode.permission import Mode, Outcome, new_engine
from helpcode.subagent.catalog import load_catalog
from helpcode.subagent.runner import ParentContext, SubAgentRunner
from helpcode.subagent.task import TaskManager
from helpcode.team import (
    Manager,
    TeamError,
    TeamHasActiveMembersError,
    TeamNotFoundError,
)
from helpcode.team.service import TeamService
from helpcode.tool import new_default_registry
from helpcode.worktree import Manager as WorktreeManager


class _Provider:
    def __init__(self, rounds):
        self.rounds = list(rounds)

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "m"

    async def stream(self, req):
        events = self.rounds.pop(0) if self.rounds else [StreamEvent(text="完成")]
        for ev in events:
            yield ev


def _text(text="完成"):
    return [StreamEvent(text=text), StreamEvent(done=True)]


class _Env:
    def __init__(self, svc, runner, mgr, repo, team):
        self.svc, self.runner, self.mgr, self.repo, self.team = (
            svc,
            runner,
            mgr,
            repo,
            team,
        )


@pytest.fixture
def env(tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt = WorktreeManager(repo)
    mgr = Manager(tmp_path / "home", wt)
    engine, _ = new_engine(str(repo))
    parent = ParentContext(
        provider=_Provider([_text(), _text(), _text(), _text()]),
        provider_cfg=None,
        engine=engine,
        hooks=None,
        version="test",
        registry_factory=new_default_registry,
        context_factory=NullContext,
        conv_getter=lambda: Conversation(),
        approval_handler=None,
    )
    catalog, _ = load_catalog(repo, repo)
    runner = SubAgentRunner(
        catalog, TaskManager(), parent, auto_background=5.0, worktrees=wt
    )
    svc = TeamService(mgr, runner, mode_getter=lambda: Mode.DEFAULT)
    team = svc.create_team("refactor", "重构四个模块")
    return _Env(svc, runner, mgr, repo, team)


def _role(env, name="general-purpose"):
    return env.runner.catalog.get(name) or env.runner.catalog.all()[0]


def test_spawn_creates_worktree(env):
    async def scenario():
        report = await env.svc.spawn(
            env.team.sanitized_name, _role(env), "写个文件", member_name="alice"
        )
        await env.runner.tasks.drain()
        return report

    report = asyncio.run(scenario())

    assert Path(report["worktree"]).is_dir()
    assert report["branch"].startswith("worktree-refactor+alice")
    assert "worktree-refactor+alice" in git(env.repo, "branch", "--list", "worktree-*")
    member = env.team.member("alice")
    assert member is not None and member.worktree_path == report["worktree"]


def test_spawn_sets_agent_cwd_and_inbox(env):
    async def scenario():
        await env.svc.spawn(
            env.team.sanitized_name, _role(env), "干活", member_name="bob"
        )
        await env.runner.tasks.drain()

    asyncio.run(scenario())
    runtime = env.svc._runtime["bob"]
    assert runtime.agent.cwd == runtime.worktree
    assert runtime.agent._inbox is not None
    assert runtime.conv is not None


def test_idle_notification(env):
    async def scenario():
        await env.svc.spawn(
            env.team.sanitized_name, _role(env), "干活", member_name="carol"
        )
        await env.runner.tasks.drain()

    asyncio.run(scenario())
    assert env.team.member("carol").is_active is False
    unread = env.svc.mailbox(env.team).peek_unread("lead")
    assert any(m.type == "idle_notification" and m.sender == "carol" for m in unread)


def test_resume_restores_history(env):
    async def scenario():
        await env.svc.spawn(
            env.team.sanitized_name, _role(env), "第一件事", member_name="dave"
        )
        await env.runner.tasks.drain()
        report = await env.svc.resume("dave", "再改一下")
        await env.runner.tasks.drain()
        return report

    report = asyncio.run(scenario())
    # 历史里至少有「第一件事 + 它的回答」，续派不是从零开始
    assert report["history"] >= 2
    assert len(env.svc._runtime["dave"].conv.messages()) >= 4


def test_plan_approval_flow(env):
    async def scenario():
        await env.svc.spawn(
            env.team.sanitized_name,
            _role(env),
            "先给计划",
            member_name="erin",
            plan_mode_required=True,
        )
        await env.runner.tasks.drain()
        pending = env.svc.mailbox(env.team).peek_unread("lead")
        assert any(m.type == "plan_approval_request" for m in pending)
        assert env.svc._runtime["erin"].mode is Mode.PLAN

        result = await env.svc.send(
            "lead", "erin", "同意", type="plan_approval_response", approve=True
        )
        await env.runner.tasks.drain()
        return result

    result = asyncio.run(scenario())
    assert result["resumed"] == ["erin"]
    assert env.svc._runtime["erin"].mode is Mode.DEFAULT


def test_plan_rejected_keeps_plan_mode(env):
    async def scenario():
        await env.svc.spawn(
            env.team.sanitized_name,
            _role(env),
            "先给计划",
            member_name="frank",
            plan_mode_required=True,
        )
        await env.runner.tasks.drain()

    asyncio.run(scenario())
    assert env.svc._runtime["frank"].mode is Mode.PLAN


def test_spawn_errors(env):
    async def scenario():
        with pytest.raises(TeamNotFoundError):
            await env.svc.spawn("no-such-team", _role(env), "x")
        await env.svc.spawn(
            env.team.sanitized_name, _role(env), "x", member_name="gina"
        )
        await env.runner.tasks.drain()
        with pytest.raises(TeamError):
            await env.svc.spawn(
                env.team.sanitized_name, _role(env), "x", member_name="gina"
            )

    asyncio.run(scenario())


def test_member_registry(env):
    registry = env.svc.member_registry(_role(env), "hank")
    names = set(registry.names())
    assert {"TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "SendMessage"} <= names
    assert "Agent" not in names  # 全局禁止：子 Agent 不能无限嵌套
    assert "TeamCreate" not in names
    assert "TeamDelete" not in names


def test_delete_requires_idle(env):
    async def scenario():
        await env.svc.spawn(
            env.team.sanitized_name, _role(env), "长任务", member_name="ivy"
        )
        with pytest.raises(TeamHasActiveMembersError):
            env.svc.delete_team(env.team.sanitized_name)
        await env.runner.tasks.drain()

    asyncio.run(scenario())
    # 空闲之后就能删了
    report = env.svc.delete_team(env.team.sanitized_name)
    assert report["removed_members"] == ["ivy"]


def test_force_delete(env):
    async def scenario():
        await env.svc.spawn(
            env.team.sanitized_name, _role(env), "长任务", member_name="jack"
        )
        report = env.svc.delete_team(env.team.sanitized_name, force=True)
        await env.runner.tasks.drain()
        return report

    report = asyncio.run(scenario())
    assert report["removed_members"] == ["jack"]
    assert env.mgr.get(env.team.sanitized_name) is None
    assert not Path(env.team.config_dir).exists()


def test_send_rejects_unknown_recipient(env):
    async def scenario():
        with pytest.raises(TeamNotFoundError):
            await env.svc.send("lead", "nobody", "hi")

    asyncio.run(scenario())


def test_member_build_allows_approval(env, monkeypatch):
    """派活构造队员时必须允许升级审批。

    队员是「后台任务」，但主 TUI 就在同一进程——把 background 当成
    「没人可问」会让 it 的写操作永久被拒（实测：队员只会复述
    「文件写入被拒绝，让我改用 bash」，然后这一轮就结束）。
    """
    seen: dict = {}
    original = env.runner.build

    def spy(role, **kwargs):
        seen.update(kwargs)
        return original(role, **kwargs)

    monkeypatch.setattr(env.runner, "build", spy)

    async def scenario():
        await env.svc.spawn(
            env.team.sanitized_name, _role(env), "干活", member_name="eve"
        )
        # 必须收尾再退出：loop 关闭时取消挂起的后台任务会卡在生成器收尾上
        await env.runner.tasks.drain()

    asyncio.run(scenario())
    assert seen.get("background") is True
    assert seen.get("can_approve") is True


def test_member_write_file_asks_and_succeeds(tmp_path):
    """队员撞 ASK 能升级审批，批准后文件真的写进自己的 worktree。

    复现用户实测那一次：修复前 approval 被换成「一律拒绝」，
    队员永远写不出文件，只会复述「文件写入被拒绝，让我改用 bash」。
    """
    repo = make_repo(tmp_path / "repo")
    wt = WorktreeManager(repo)
    mgr = Manager(tmp_path / "home", wt)
    engine, _ = new_engine(str(repo))
    asked: list[str] = []

    async def approve(call, reason):
        asked.append(call.name)
        return Outcome.ALLOW_ONCE

    rounds = [
        [
            StreamEvent(
                done=True,
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="write_file",
                        input={"path": "alice.txt", "content": "alice\n"},
                    )
                ],
            )
        ],
        _text("写好了"),
    ]
    parent = ParentContext(
        provider=_Provider(rounds),
        provider_cfg=None,
        engine=engine,
        hooks=None,
        version="test",
        registry_factory=new_default_registry,
        context_factory=NullContext,
        conv_getter=lambda: Conversation(),
        approval_handler=approve,
    )
    catalog, _ = load_catalog(repo, repo)
    runner = SubAgentRunner(
        catalog, TaskManager(), parent, auto_background=5.0, worktrees=wt
    )
    svc = TeamService(mgr, runner, mode_getter=lambda: Mode.DEFAULT)
    team = svc.create_team("refactor", "写文件")

    async def scenario():
        report = await svc.spawn(
            team.sanitized_name,
            catalog.get("general-purpose"),
            "写 alice.txt",
            member_name="alice",
        )
        await runner.tasks.drain()
        return report

    report = asyncio.run(scenario())

    assert asked == ["write_file"]  # Ask 真的升级上去了
    target = Path(report["worktree"]) / "alice.txt"
    assert target.read_text(encoding="utf-8") == "alice\n"
