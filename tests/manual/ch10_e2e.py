"""ch10 端到端：真 provider + 真 TUI，跑一遍核心命令流。

不发模型的本地命令（/status /help /permission /plan）与发模型的提示词命令
（/do /review）都走真实 dispatch 链路。

用法：在仓库根 `python tests/manual/ch10_e2e.py`（读 .helpcode/config.yaml）。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from helpcode.command import build_default_registry
from helpcode.config import load
from helpcode.permission import Mode
from helpcode.tui import SessionState
from helpcode.tui.app import HelpCodeApp


async def _wait_idle(app, timeout=300):
    for _ in range(timeout):
        if app.state is SessionState.IDLE:
            return True
        await asyncio.sleep(0.1)
    return False


async def main() -> None:
    os.chdir(tempfile.mkdtemp(prefix="hc-ch10-"))
    cfg = load(str(REPO / ".helpcode" / "config.yaml"))
    app = HelpCodeApp(cfg.providers, commands=build_default_registry())

    async with app.run_test() as pilot:
        await pilot.pause()

        await app.submit("/help")
        await pilot.pause()
        help_lines = app.query_one("#log").lines
        assert any("/status" in str(l) for l in help_lines), "帮助列表该含 /status"

        await app.submit("/status")
        await pilot.pause()
        assert any("模型" in str(l) for l in app.query_one("#log").lines)

        await app.submit("/permission plan")
        await pilot.pause()
        assert app.mode == Mode.PLAN

        await app.submit("/do")
        await asyncio.sleep(2)  # 让流跑起来（/do 会触发模型按计划执行，可能多轮）
        do_msgs = len(app.conv.messages())
        print("  /do 触发，消息数:", do_msgs, "状态:", app.state.name)
        if app.state is not SessionState.IDLE:
            await app.action_cancel()
        assert do_msgs >= 1, "/do 应把执行指令作为用户消息写入历史"

        before = len(app.conv.messages())
        await app.submit("/review src/helpcode/command")
        await asyncio.sleep(2)
        print(
            "  /review 触发，消息数:", len(app.conv.messages()), "状态:", app.state.name
        )
        if app.state is not SessionState.IDLE:
            await app.action_cancel()
        assert len(app.conv.messages()) > before, "审查提示词应作为用户消息写入历史"

    print("CH10 E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
