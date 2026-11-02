"""ch09 端到端子进程：真 provider 跑真实对话，然后停在那儿等被硬杀。

主进程（tmp_e2e_driver.py）负责 Stop-Process -Force / kill -9 等价的强制终止，
本进程不做任何收尾——这正是要验的东西：不给程序 close() 的机会，
看磁盘上到底留下了什么。

用法：cwd 设为临时项目目录，环境变量 E2E_REPO 指向仓库根。
"""

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(os.environ["E2E_REPO"])
sys.path.insert(0, str(REPO / "src"))

from helpcode import cli
from helpcode.agent import Agent
from helpcode.config import load
from helpcode.context import ContextManager
from helpcode.llm import new_provider
from helpcode.permission import Mode, new_engine
from helpcode.tool import new_default_registry


async def main(mode_arg: str) -> None:
    root = Path.cwd()
    cfg = load(str(REPO / ".helpcode" / "config.yaml"))
    pcfg = cfg.providers[0]
    provider = new_provider(pcfg)

    conv, writer, session_id, _ = cli._build_conversation(root, None)
    writer.set_model(pcfg.model)
    ctx = ContextManager(provider, root, pcfg.context_window)
    engine, _ = new_engine(str(root))
    agent = Agent(provider, new_default_registry(), "e2e", engine, ctx)

    # 长文本必须放在**中间**而不是第一条：find_cut 从尾部向前凑够 KEEP_TAIL_TOKENS
    # 就停，长消息若在 index 0，一累加就走到头（i==0）→ 判定「没有可摘要的部分」。
    filler = "这是一段用于测试压缩的长文本。" * 1200
    print(f"SESSION {session_id}", flush=True)
    for question in (
        "请只回答两个字：苹果",
        f"请阅读下面的材料，然后只回答两个字：香蕉\n\n{filler}",
        "请只回答两个字：橘子",
        "请只回答两个字：葡萄",
    ):
        async for _event in agent.run(question, conv, asyncio.Event(), Mode.DEFAULT):
            pass
    print(f"ROUNDS_DONE {len(conv.messages())}", flush=True)

    if mode_arg in ("compact", "compact-exit"):
        report = await agent.compact_now(conv)
        head = conv.messages()[0].content[:40] if conv.messages() else ""
        print(
            f"COMPACT_DONE|{len(conv.messages())}|{head!r}|{report.notice}", flush=True
        )
        if mode_arg == "compact-exit":
            writer.close()
            print("EXITED", flush=True)
            return

    # 不 close、不 exit：让主进程从外部杀掉
    await asyncio.sleep(300)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "plain"))
