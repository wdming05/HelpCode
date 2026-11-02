"""Team 相关测试的共用工装：临时仓库 + 假 provider + 真 Worktree。

团队功能横跨 worktree / subagent / 邮箱，测试里必须都用真家伙，
只把模型换成回放器——否则测的是 mock 的协作。
"""

from __future__ import annotations

from types import SimpleNamespace

from git_repo import make_repo
from null_context import NullContext

from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent
from helpcode.permission import Mode, new_engine
from helpcode.subagent.catalog import load_catalog
from helpcode.subagent.runner import ParentContext, SubAgentRunner
from helpcode.subagent.task import TaskManager
from helpcode.team import Manager, TeamService
from helpcode.tool import new_default_registry
from helpcode.worktree import Manager as WorktreeManager


class FakeProvider:
    """按轮回放；rounds 用完就一直回「完成」。"""

    def __init__(self, rounds: int = 4):
        self.rounds = [
            [StreamEvent(text=f"结果{i}"), StreamEvent(done=True)]
            for i in range(rounds)
        ]

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
        if not any(getattr(ev, "done", False) for ev in events):
            yield StreamEvent(done=True)


def build(tmp_path, *, rounds: int = 4, team_name: str = "refactor"):
    repo = make_repo(tmp_path / "repo")
    wt = WorktreeManager(repo)
    mgr = Manager(tmp_path / "home", wt)
    engine, _ = new_engine(str(repo))
    parent = ParentContext(
        provider=FakeProvider(rounds),
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
    tasks = TaskManager()
    runner = SubAgentRunner(catalog, tasks, parent, auto_background=5.0, worktrees=wt)
    svc = TeamService(mgr, runner, mode_getter=lambda: Mode.DEFAULT)
    team = svc.create_team(team_name, "重构四个模块")
    return SimpleNamespace(
        repo=repo,
        wt=wt,
        mgr=mgr,
        runner=runner,
        tasks=tasks,
        svc=svc,
        team=team,
        catalog=catalog,
    )


def role(env, name: str = "general-purpose"):
    return env.catalog.get(name) or env.catalog.all()[0]
