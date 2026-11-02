"""ch11 端到端：Skill 两阶段加载 / 激活 / 白名单 / 斜杠短命令 / 清空。

走真实 HelpCodeApp（run_test）与真实 SkillManager / 命令注册表，但用 FakeProvider
替代网络调用——本章验证的是「加载/激活/收窄/分流」这条管线，不是模型是否听话。
三个场景对应 checklist 的端到端项。

运行：.venv/Scripts/python.exe tests/manual/ch11_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from helpcode.agent import Agent
from helpcode.command import build_default_registry
from helpcode.config import ProviderConfig
from helpcode.llm import StreamEvent
from helpcode.permission import Mode, new_engine
from helpcode.skill import (
    SkillManager,
    SkillTool,
    load_skills,
    register_skill_commands,
)
from helpcode.skill.registry import SkillRegistry
from helpcode.skill.types import ExecutionMode, Skill
from helpcode.tool import Registry
from helpcode.tui import SessionState
from helpcode.tui.app import HelpCodeApp

_CFG = [ProviderConfig(name="fake", protocol="openai", api_key="x", model="m")]


class FakeProvider:
    name = "fake"
    model = "m"

    def __init__(self):
        self.n = 0
        self.requests = []

    async def stream(self, req):
        self.n += 1
        self.requests.append(req)
        yield StreamEvent(text="已按 SOP 完成")
        yield StreamEvent(done=True)


def _setup(app, fp, manager):
    app.provider = fp
    app.provider_cfg = _CFG[0]
    app.engine, _ = new_engine(tempfile.mkdtemp())
    app.agent = Agent(fp, Registry(), "dev", app.engine, _NullContext())
    app.commands = build_default_registry()
    register_skill_commands(app.commands, manager)
    app.skill_manager = manager
    app.mode = Mode.DEFAULT
    app.state = SessionState.IDLE


class _NullContext:
    context_window = 128000

    def estimate(self, conv):
        return 0

    def should_compress(self, conv):
        return False

    def after_stream(self, usage, message_count):
        return None

    def has_room_after_compress(self, tokens_after):
        return True

    async def before_request(self, conv, cancel_event, tool_names):
        from helpcode.context import CompressReport

        return CompressReport()

    async def compact_now(self, conv, emergency=False, tool_names=None):
        from helpcode.context import CompressReport

        return CompressReport(notice="无需压缩")


async def _wait_idle(app, timeout=300):
    for _ in range(timeout):
        if app.state is SessionState.IDLE:
            return True
        await asyncio.sleep(0.01)
    return False


async def _run():
    work = Path(tempfile.mkdtemp())
    os.chdir(work)
    manager = SkillManager(load_skills(work, work / "home"))

    app = HelpCodeApp(_CFG)
    fp = FakeProvider()
    async with app.run_test() as pilot:
        await pilot.pause()
        _setup(app, fp, manager)

        # 场景 1a：/skills 列出三个内置样板
        await app.submit("/skills")
        await pilot.pause()
        log = "\n".join(str(l) for l in app.query_one("#log").lines)
        for name in ("commit", "review", "test"):
            assert name in log, f"/skills 应列出 {name}"
        print("  场景1a 通过：/skills 列出三样板")

        # 场景 1b：load_skill(review) 激活 + 自动注册短命令
        tool = SkillTool(manager, app._register_skill_command)
        result = tool.run("review")
        assert result.ok and "资深代码评审员" in result.content
        assert manager.is_active("review")
        # review 与 ch10 /review 撞名：短命令不注册（仍是 ch10 内置），但有冲突提示
        assert app.commands.resolve("review") is not None  # ch10 内置仍在
        log = "\n".join(str(l) for l in app.query_one("#log").lines)
        assert "冲突" in log, "撞名应有提示"
        print("  场景1b 通过：load_skill 激活（review 撞名，短命令被跳过）")

        tool.run("commit")
        assert app.commands.resolve("commit") is not None  # 无撞名 → 注册 /commit
        print("  场景1b 通过：/commit 短命令已注册")

        # 场景 1c：/skills 显示已激活
        await app.submit("/skills")
        await pilot.pause()
        log = "\n".join(str(l) for l in app.query_one("#log").lines)
        assert "已激活" in log and "/commit" in log and "/review" in log
        print("  场景1c 通过：/skills 显示已激活")

        # 场景 1d：/commit 触发 shared 模式，SOP 送进对话
        await app.submit("/commit 修了登录 bug")
        assert await _wait_idle(app)
        users = [m.content for m in app.conv.messages() if m.role == "user"]
        assert any("修了登录 bug" in c for c in users)
        assert fp.n == 1
        print("  场景1d 通过：/commit 把替换后的 SOP 送进对话")

        # 场景 3：isolated Skill 独立跑完只回流摘要
        reg = SkillRegistry()
        reg.set(
            Skill(
                name="iso",
                description="独立",
                body="结论：{{args}}",
                mode=ExecutionMode.ISOLATED,
            )
        )
        app.skill_manager._registry = reg
        app.skill_manager.activate("iso")
        result = await app._run_skill("iso", "独立结论")
        assert "[Skill: iso]" in result
        assert (
            len(app.conv.messages()) == len(users) + 2
        )  # 原 user+assistant + 一条摘要
        print("  场景3 通过：isolated 只回流一条摘要")

        # 场景 2：/clear 清空激活
        await app.submit("/clear")
        await pilot.pause()
        assert manager.render_active() == ""
        print("  场景2 通过：/clear 清空已激活 Skill")

    print("CH11 E2E OK")


if __name__ == "__main__":
    asyncio.run(_run())
