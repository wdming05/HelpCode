"""ch15 端到端：建队 → 派两个队员 → 空闲通知 → 续派 → 合并 → 删队。

跑法（项目根目录）：
    .venv/Scripts/python.exe tests/manual/ch15_e2e.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from git_repo import git, make_repo

from helpcode.conversation import Conversation
from helpcode.llm import StreamEvent, ToolCall
from helpcode.permission import Mode, new_engine
from helpcode.subagent.catalog import Role, load_catalog
from helpcode.subagent.runner import ParentContext, SubAgentRunner
from helpcode.subagent.task import TaskManager
from helpcode.team import Manager as TeamManager
from helpcode.team.service import TeamService
from helpcode.tool import new_default_registry
from helpcode.worktree import Manager as WorktreeManager

TMP = Path(tempfile.mkdtemp(prefix="ch15-"))


class _NullContext:
    """成员用的空 ContextManager：e2e 不压缩。"""

    def estimate(self, conv):
        return 0

    def should_compress(self, conv):
        return False

    async def before_request(self, conv, cancel_event, tool_names):
        class _R:
            cancelled = False
            notice = ""
            info = None

        return _R()

    async def after_stream(self, usage, size):
        return None


class _Provider:
    """按序回放。回放顺序与 spawn 顺序一一对应（顺序 spawn 保证确定性）。"""

    def __init__(self, rounds):
        self.rounds = list(rounds)

    @property
    def name(self):
        return "e2e"

    @property
    def model(self):
        return "m"

    async def stream(self, req):
        events = self.rounds.pop(0) if self.rounds else [StreamEvent(text="完成")]
        for ev in events:
            yield ev


def _write_round(filename: str):
    return [
        StreamEvent(
            done=True,
            tool_calls=[
                ToolCall(
                    id=f"w-{filename}",
                    name="write_file",
                    input={"path": filename, "content": f"来自 {filename}\n"},
                )
            ],
        )
    ]


def _commit_round(message: str):
    """队员提交自己的工作——不提交就没有 commit 可合并。"""
    return [
        StreamEvent(
            done=True,
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="bash",
                    input={"command": f'git add -A && git commit -m "{message}"'},
                )
            ],
        )
    ]


def _text_round(text: str):
    return [StreamEvent(text=text), StreamEvent(done=True)]


MEMBER_ROLE = Role(
    name="teammate",
    description="能写文件的队员",
    body="你在自己的 worktree 里干活，只用相对路径。",
    tools=("read_file", "write_file", "bash", "glob", "grep"),
    permission_mode="bypassPermissions",
)


def main() -> None:
    repo = make_repo(TMP / "repo")
    home = TMP / "home"

    wt = WorktreeManager(repo)
    mgr = TeamManager(home, wt)
    engine, _ = new_engine(str(repo))
    parent = ParentContext(
        provider=_Provider(
            [
                _write_round("alice.txt"),
                _commit_round("alice: add file"),
                _text_round("alice 完成"),
                _write_round("bob.txt"),
                _commit_round("bob: add file"),
                _text_round("bob 完成"),
                _text_round("alice 又改了一版"),
            ]
        ),
        provider_cfg=None,
        engine=engine,
        hooks=None,
        version="e2e",
        registry_factory=new_default_registry,
        context_factory=_NullContext,
        conv_getter=lambda: Conversation(),
        approval_handler=None,
    )
    catalog, _ = load_catalog(repo, repo)
    tasks = TaskManager()
    runner = SubAgentRunner(catalog, tasks, parent, auto_background=20.0, worktrees=wt)
    svc = TeamService(mgr, runner, mode_getter=lambda: Mode.DEFAULT)
    team = svc.create_team("refactor", "两个模块各改一处")

    # ---------------- 场景 1：派两个队员，各写各的 worktree
    async def scenario_1():
        await svc.spawn(
            team.sanitized_name, MEMBER_ROLE, "创建 alice.txt", member_name="alice"
        )
        await tasks.drain()
        await svc.spawn(
            team.sanitized_name, MEMBER_ROLE, "创建 bob.txt", member_name="bob"
        )
        await tasks.drain()

    asyncio.run(scenario_1())

    alice_wt = Path(team.member("alice").worktree_path)
    bob_wt = Path(team.member("bob").worktree_path)
    assert (alice_wt / "alice.txt").is_file(), "alice 的文件应落在自己的 worktree"
    assert (bob_wt / "bob.txt").is_file(), "bob 的文件应落在自己的 worktree"
    assert not (repo / "alice.txt").exists() and not (repo / "bob.txt").exists()
    print("  场景1 通过：两个队员各写各的 worktree，主目录干净")

    # ---------------- 场景 2：空闲通知 + 续派
    kinds = [m.type for m in svc.mailbox(team).peek_unread("lead")]
    assert kinds.count("idle_notification") >= 2, kinds
    assert team.member("alice").is_active is False
    print("  场景2a 通过：两名队员停下后 Lead 收到空闲通知")

    async def scenario_2():
        report = await svc.resume("alice", "再补一句说明")
        await tasks.drain()
        return report

    report = asyncio.run(scenario_2())
    assert report["history"] >= 2, report
    print(f"  场景2b 通过：续派沿用历史（{report['history']} 条）")

    # ---------------- 场景 3：Lead 用 bash 合并分支，然后删队
    for member in ("alice", "bob"):
        git(
            repo,
            "merge",
            "--no-ff",
            team.member(member).branch,
            "-m",
            f"merge: {member}",
        )
    assert (repo / "alice.txt").is_file() and (repo / "bob.txt").is_file()
    print("  场景3a 通过：两个分支合并进主目录")

    report = svc.delete_team(team.sanitized_name, force=True)
    assert report["removed_members"] == ["alice", "bob"], report
    assert mgr.get(team.sanitized_name) is None
    assert not alice_wt.exists() and not bob_wt.exists()
    print("  场景3b 通过：删队清掉了 worktree 与配置目录")

    print("CH15 E2E OK")


if __name__ == "__main__":
    main()
