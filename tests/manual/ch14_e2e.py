"""ch14 端到端：隔离真的隔离了吗、删除真的拦住改动了吗。

跑法（项目根目录）：
    .venv/Scripts/python.exe tests/manual/ch14_e2e.py
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
from helpcode.permission import new_engine
from helpcode.subagent.catalog import Role, load_catalog
from helpcode.subagent.runner import ParentContext, SubAgentRunner
from helpcode.subagent.task import TaskManager
from helpcode.subagent.tools import AgentTool
from helpcode.tool import new_default_registry
from helpcode.worktree import (
    Manager,
    NotARepoError,
    WorktreeHasChangesError,
)

TMP = Path(tempfile.mkdtemp(prefix="ch14-"))


class _Provider:
    """照脚本回放：先要求写文件，再给一句总结。"""

    def __init__(self, rounds):
        self.rounds = list(rounds)

    @property
    def name(self):
        return "e2e"

    @property
    def model(self):
        return "m"

    async def stream(self, req):
        events = self.rounds.pop(0) if self.rounds else [StreamEvent(done=True)]
        for ev in events:
            yield ev


def _write_then_text(filename: str):
    return [
        [
            StreamEvent(
                done=True,
                tool_calls=[
                    ToolCall(
                        id="w1",
                        name="write_file",
                        input={"path": filename, "content": "来自子 Agent\n"},
                    )
                ],
            )
        ],
        [StreamEvent(text="已写入 " + filename), StreamEvent(done=True)],
    ]


def _runner(provider, repo: Path, worktrees: Manager):
    engine, _ = new_engine(str(repo))
    parent = ParentContext(
        provider=provider,
        provider_cfg=None,
        engine=engine,
        hooks=None,
        version="e2e",
        registry_factory=new_default_registry,
        context_factory=lambda: _NullContext(),
        conv_getter=lambda: Conversation(),
        approval_handler=None,
    )
    catalog, _ = load_catalog(repo, repo)
    return SubAgentRunner(
        catalog, TaskManager(), parent, auto_background=10.0, worktrees=worktrees
    )


class _NullContext:
    """子 Agent 的 ContextManager 空壳：e2e 不压缩。"""

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


ISO_ROLE = Role(
    name="iso",
    description="隔离执行的子 Agent",
    body="你只会往当前目录写文件",
    tools=("write_file", "read_file"),
    isolation="worktree",
    permission_mode="bypassPermissions",
)


def main() -> None:
    repo = make_repo(TMP / "repo")
    worktrees = Manager(repo)

    # ---------------- 场景 1：隔离真的隔离
    runner = _runner(_Provider(_write_then_text("from-subagent.txt")), repo, worktrees)
    runner.catalog.set(ISO_ROLE)
    tool = AgentTool(runner)
    result = asyncio.run(
        tool.arun("写一个文件", description="写文件", subagent_type="iso")
    )
    assert not result.is_error, result.content

    dirs = [p for p in worktrees.worktree_dir.iterdir() if p.is_dir()]
    assert len(dirs) == 1, f"应只有一个临时 Worktree，实际 {dirs}"
    wt = dirs[0]
    assert (wt / "from-subagent.txt").is_file(), "文件应落在 Worktree 里"
    assert not (repo / "from-subagent.txt").exists(), "主工作目录不该被动过"
    assert "Worktree 保留在" in result.content, result.content
    print(f"  场景1 通过：写入只落在 {wt.name}，主目录干净")

    # ---------------- 场景 2：变更保护
    name = wt.name
    try:
        worktrees.remove(name)
        raise AssertionError("有改动时不该允许删除")
    except WorktreeHasChangesError:
        print("  场景2a 通过：有改动时删除被拒")

    report = worktrees.remove(name, discard_changes=True)
    assert report.removed and not Path(report.path).exists()
    assert "worktree-" + name not in git(repo, "branch", "--list", "worktree-*")
    print("  场景2b 通过：--discard 后目录与分支都消失")

    # ---------------- 场景 3：非 git 目录降级
    plain = TMP / "plain"
    plain.mkdir()
    try:
        Manager(plain)
        raise AssertionError("非仓库不应构造成功")
    except NotARepoError as e:
        print(f"  场景3 通过：非仓库降级（{e.__class__.__name__}）")

    print("CH14 E2E OK")


if __name__ == "__main__":
    main()
