"""ch09 场景 1/2：三层指令文件 + @ 引用，真模型复述。

不靠「渲染结果里有这行字」自证——直接问模型，看它复述出来的内容是不是
三个不同的暗号加一个被引用文件里的暗号。顺序也要对：项目根在最前。
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
from helpcode.memory import instructions as ins
from helpcode.permission import Mode, new_engine
from helpcode.prompt import build_system_prompt
from helpcode.tool import new_default_registry

ALPHA = "暗号项目根是ALPHA"
BETA = "暗号配置层是BETA"
GAMMA = "暗号用户层是GAMMA"
DELTA = "暗号被引用文件是DELTA"


async def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="hc-instr-"))
    root = work / "proj"
    home = work / "home"
    (root / "docs").mkdir(parents=True)
    (root / ".helpcode" / "memory").mkdir(parents=True)
    (home / ".helpcode" / "memory").mkdir(parents=True)

    (root / "AGENTS.md").write_text(f"{ALPHA}\n@docs/STYLE.md\n", encoding="utf-8")
    (root / "docs" / "STYLE.md").write_text(DELTA, encoding="utf-8")
    (root / ".helpcode" / "AGENTS.md").write_text(BETA, encoding="utf-8")
    (home / ".helpcode" / "AGENTS.md").write_text(GAMMA, encoding="utf-8")

    instruction_set = ins.load(root, home)
    bundle = index_mod.load(
        home / ".helpcode" / "memory", root / ".helpcode" / "memory"
    )
    system = build_system_prompt(instruction_set.render(), index_mod.render(bundle))
    print("   指令段告警:", instruction_set.warnings)
    for code in (ALPHA, BETA, GAMMA, DELTA):
        print(f"   system 含 {code}:", code in system)
    order = [system.index(c) for c in (ALPHA, DELTA, BETA, GAMMA)]
    print("   四个暗号的位置:", order, "递增:", order == sorted(order))

    cfg = load(str(REPO / ".helpcode" / "config.yaml"))
    pcfg = cfg.providers[0]
    provider = new_provider(pcfg)
    ctx = ContextManager(provider, work, pcfg.context_window)
    engine, _ = new_engine(str(work))
    agent = Agent(provider, new_default_registry(), "e2e", engine, ctx)
    agent._system_extra = (instruction_set.render(), index_mod.render(bundle))

    answer = ""
    async for event in agent.run(
        "请把你看到的暗号逐条列出来，不要解释。",
        Conversation(),
        asyncio.Event(),
        Mode.DEFAULT,
    ):
        answer += event.text or ""

    print("   模型回答:", answer.strip()[:300])
    for code in (ALPHA, BETA, GAMMA, DELTA):
        print(f"   模型复述 {code}:", code in answer)


if __name__ == "__main__":
    os.chdir(tempfile.mkdtemp(prefix="hc-instr-cwd-"))
    asyncio.run(main())
