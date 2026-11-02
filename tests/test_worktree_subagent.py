"""SubAgent 隔离：isolation 字段、cwd 落到 Worktree、跑完自动清理。"""

import asyncio
from pathlib import Path

import pytest
from git_repo import make_repo
from null_context import NullContext

from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent, ToolCall
from helpcode.permission import new_engine
from helpcode.subagent.catalog import Role, load_catalog, parse_role
from helpcode.subagent.runner import ParentContext, SubAgentRunner
from helpcode.subagent.task import TaskManager
from helpcode.subagent.tools import AgentTool
from helpcode.tool import new_default_registry
from helpcode.worktree import Manager


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
        events = (
            self.rounds.pop(0) if self.rounds else [StreamEvent(text="", done=True)]
        )
        for ev in events:
            yield ev


def _text_round(text="结果"):
    return [StreamEvent(text=text), StreamEvent(done=True)]


def _write_round(path="note.txt", content="hi"):
    return [
        StreamEvent(
            done=True,
            tool_calls=[
                ToolCall(
                    id="w1", name="write_file", input={"path": path, "content": content}
                )
            ],
        )
    ]


ISO_ROLE = Role(
    name="iso",
    description="隔离执行",
    body="你是隔离执行的子 Agent",
    tools=("write_file", "read_file"),
    isolation="worktree",
    permission_mode="bypassPermissions",
)


def _runner(provider, repo, worktrees, on_done=None):
    engine, _ = new_engine(str(repo))
    parent = ParentContext(
        provider=provider,
        provider_cfg=None,
        engine=engine,
        hooks=None,
        version="test",
        registry_factory=new_default_registry,
        context_factory=NullContext,
        conv_getter=lambda: Conversation(),
        approval_handler=None,
    )
    catalog, _ = load_catalog(Path(repo), Path(repo))
    tasks = TaskManager(on_done=on_done)
    return SubAgentRunner(
        catalog, tasks, parent, auto_background=5.0, worktrees=worktrees
    )


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


def _worktree_dirs(m: Manager):
    return [p for p in m.worktree_dir.iterdir() if p.is_dir()]


def test_role_isolation_parsed():
    text = "---\nname: iso\ndescription: d\nisolation: worktree\n---\n正文"
    role, err = parse_role(text)
    assert err is None
    assert role.isolation == "worktree"


def test_role_isolation_invalid_falls_back():
    text = "---\nname: iso\ndescription: d\nisolation: docker\n---\n正文"
    role, _ = parse_role(text)
    assert role.isolation == ""
    # 不写该字段时也是空
    role2, _ = parse_role("---\nname: x\ndescription: d\n---\n正文")
    assert role2.isolation == ""


def test_runner_builds_with_isolation(repo):
    """build 阶段不建 Worktree（隔离发生在 run），Agent 能正常造出来。"""
    m = Manager(repo)
    runner = _runner(_Provider([_text_round()]), repo, m)
    agent, conv = runner.build(ISO_ROLE, background=False)
    assert agent.cwd == str(Path.cwd())
    assert conv is not None


def test_isolated_writes_stay_in_worktree(repo):
    m = Manager(repo)
    runner = _runner(_Provider([_write_round(), _text_round("写好了")]), repo, m)

    result = asyncio.run(runner.run(ISO_ROLE, "写 note.txt", background=False))

    assert result["status"] == "completed"
    dirs = _worktree_dirs(m)
    assert len(dirs) == 1
    assert (dirs[0] / "note.txt").read_text(encoding="utf-8") == "hi"
    # 主仓库工作目录完全没被碰
    assert not (repo / "note.txt").exists()


def test_dirty_run_keeps_and_reports(repo):
    m = Manager(repo)
    runner = _runner(_Provider([_write_round(), _text_round("写好了")]), repo, m)
    result = asyncio.run(runner.run(ISO_ROLE, "写文件", background=False))
    assert "Worktree 保留在" in result["result"]
    assert "worktree-agent-a" in result["result"]
    assert _worktree_dirs(m)  # 有改动 → 目录保留


def test_clean_run_removes_worktree(repo):
    m = Manager(repo)
    runner = _runner(_Provider([_text_round("只是回答")]), repo, m)
    result = asyncio.run(runner.run(ISO_ROLE, "只回答", background=False))

    assert result["status"] == "completed"
    assert "Worktree 保留" not in result["result"]
    assert _worktree_dirs(m) == []  # 无改动 → 清掉
    assert m.current_session is None


def test_create_failure_reports_error(repo, monkeypatch):
    m = Manager(repo)

    def boom(*args, **kwargs):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(m, "create", boom)
    runner = _runner(_Provider([_text_round()]), repo, m)
    runner.catalog.set(ISO_ROLE)  # AgentTool 按名字查目录，测试角色要登记
    result = asyncio.run(runner.run(ISO_ROLE, "任务", background=False))
    assert result["status"] == "failed"
    assert "磁盘满了" in result["error"]

    tool = AgentTool(runner)
    out = asyncio.run(tool.arun("任务", description="d", subagent_type="iso"))
    assert out.is_error is True
    assert "磁盘满了" in out.content


def test_finalize_before_notify(repo):
    """通知里必须已经带上「已保留」——否则主 Agent 永远不知道目录在哪。"""
    m = Manager(repo)
    seen: list[str] = []
    runner = _runner(
        _Provider([_write_round(), _text_round("写好了")]),
        repo,
        m,
        on_done=lambda bt: seen.append(bt.result),
    )
    asyncio.run(runner.run(ISO_ROLE, "写文件", background=False))
    assert seen and "Worktree 保留在" in seen[0]
