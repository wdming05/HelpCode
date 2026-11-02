"""ch09 场景 10：同一会话连发三条，看服务端是否返回 cached_tokens。

结论只有两种：
- 第 2、3 条的 cache_read > 0 → 缓存命中，且数值稳定
- 恒为 0 → 端点不返回该字段（不是 bug，客户端已经原样透传 usage）
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
from helpcode.permission import Mode, new_engine
from helpcode.tool import new_default_registry


async def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="hc-cache-"))
    cfg = load(str(REPO / ".helpcode" / "config.yaml"))
    pcfg = cfg.providers[0]
    provider = new_provider(pcfg)
    ctx = ContextManager(provider, work, pcfg.context_window)
    engine, _ = new_engine(str(work))
    agent = Agent(provider, new_default_registry(), "e2e", engine, ctx)
    conv = Conversation()

    rows = []
    for q in ("回答一个字：一", "回答一个字：二", "回答一个字：三"):
        usage = None
        async for event in agent.run(q, conv, asyncio.Event(), Mode.DEFAULT):
            if event.token_usage is not None:
                usage = event.token_usage
        rows.append(usage)
        print(
            f"   {q} -> in={usage.input_tokens} out={usage.output_tokens} "
            f"cache_read={usage.cache_read} cache_write={usage.cache_write}",
            flush=True,
        )

    hits = [u.cache_read for u in rows]
    print("   cache_read 序列:", hits)
    if hits[1] > 0 or hits[2] > 0:
        print("   结论：端点返回 cached_tokens，缓存命中")
        print("   稳定性:", "稳定" if hits[1] == hits[2] else "不稳定")
    else:
        print("   结论：端点未返回 cached_tokens（恒 0），无法在端到端验缓存")


if __name__ == "__main__":
    os.chdir(tempfile.mkdtemp(prefix="hc-cache-cwd-"))
    asyncio.run(main())
