"""ch09 场景 6：说「记住…」→ 自动笔记落盘 → 重启后索引里能读到。

会话内不生效（快照，N1），所以「重启后知道」要真的重新 load 一次索引。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from helpcode.agent import Agent
from helpcode.config import load
from helpcode.context import ContextManager
from helpcode.conversation import Conversation
from helpcode.llm import new_provider
from helpcode.memory import index as index_mod
from helpcode.memory import notes
from helpcode.permission import Mode, new_engine
from helpcode.tool import new_default_registry


async def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="hc-notes-"))
    root = work / "proj"
    home = work / "home"
    proj_mem = root / ".helpcode" / "memory"
    home_mem = home / ".helpcode" / "memory"
    proj_mem.mkdir(parents=True)
    home_mem.mkdir(parents=True)

    cfg = load(str(REPO / ".helpcode" / "config.yaml"))
    pcfg = cfg.providers[0]
    provider = new_provider(pcfg)
    ctx = ContextManager(provider, root, pcfg.context_window)
    engine, _ = new_engine(str(root))

    fired = []

    async def hook(conv: Conversation) -> None:
        fired.append(len(conv.messages()))
        try:
            ups = await notes.update(provider, conv.messages(), "", home_mem, proj_mem)
            print("   hook 内 update 返回:", ups)
        except Exception as exc:
            print("   hook 内 update 抛错:", type(exc).__name__, exc)

    agent = Agent(
        provider, new_default_registry(), "e2e", engine, ctx, on_turn_end=hook
    )
    conv = Conversation()
    async for _event in agent.run(
        "记住：这个项目的测试一律用 pytest-xdist 并行跑。请回复「好的」。",
        conv,
        asyncio.Event(),
        Mode.DEFAULT,
    ):
        pass
    await agent.drain_note_tasks(timeout=60)
    print("   笔记任务触发次数:", len(fired))

    files = sorted(p.name for p in proj_mem.glob("*.md") if p.name != "index.md")
    print("   项目级笔记文件:", files)
    index_text = (
        (proj_mem / "index.md").read_text(encoding="utf-8")
        if (proj_mem / "index.md").exists()
        else ""
    )
    print("   索引内容:", index_text.strip()[:300])

    # 「重启」：重新扫描两级目录
    bundle = index_mod.load(home_mem, proj_mem)
    rendered = index_mod.render(bundle)
    print("   重启后索引条数:", len(rendered.splitlines()))
    hit = "pytest" in rendered.lower() or "xdist" in rendered.lower()
    print("   重启后能看到这条偏好:", hit)


if __name__ == "__main__":
    os.chdir(tempfile.mkdtemp(prefix="hc-notes-cwd-"))
    asyncio.run(main())
