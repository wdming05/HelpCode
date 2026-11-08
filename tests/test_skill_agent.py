"""Agent 接入：system 注入激活 SOP、白名单收窄、无 skills 零差异。"""

import asyncio
import tempfile

from fake_provider import FakeProvider
from null_context import NullContext

from helpcode.agent import Agent
from helpcode.conversation import Conversation
from helpcode.permission import Mode, new_engine
from helpcode.skill import SkillManager, SkillTool, load_skills
from helpcode.skill.types import LOAD_SKILL_TOOL
from helpcode.tool import new_default_registry


class _FakeSkills:
    def __init__(self, active="", available="", allowed=None):
        self._active = active
        self._available = available
        self._allowed = allowed

    def render_active(self):
        return self._active

    def render_available(self):
        return self._available

    def allowed_tools(self):
        return self._allowed


def _run(agent, text="hi"):
    async def _go():
        out = []
        async for ev in agent.run(text, Conversation(), asyncio.Event(), Mode.DEFAULT):
            out.append(ev)
        return out

    return asyncio.run(_go())


def _agent(skills=None, registry=None):
    provider = FakeProvider(["ok"])
    engine, _ = new_engine(tempfile.mkdtemp())
    agent = Agent(
        provider,
        registry or new_default_registry(),
        "dev",
        engine,
        NullContext(),
        skills=skills,
    )
    return provider, agent


def test_agent_injects_active_skill():
    skills = _FakeSkills(active="# Skill: commit\nSOP正文", available="- commit：d")
    provider, agent = _agent(skills)
    _run(agent)
    assert "# Skill: commit" in provider.requests[0].system.stable
    assert "SOP正文" in provider.requests[0].system.stable


def test_agent_no_skills_unchanged():
    provider, agent = _agent(skills=None)
    _run(agent)
    assert "可用 Skill" not in provider.requests[0].system.stable
    assert "已激活 Skill" not in provider.requests[0].system.stable


def test_agent_narrows_tools():
    skills = _FakeSkills(allowed={"read_file", "grep", LOAD_SKILL_TOOL})
    provider, agent = _agent(skills)
    _run(agent)
    names = [t["name"] for t in provider.requests[0].tools]
    assert set(names) == {"read_file", "grep"}  # load_skill 不在 registry 里


def test_agent_no_whitelist_keeps_all():
    provider, agent = _agent(skills=_FakeSkills(allowed=None))
    _run(agent)
    names = [t["name"] for t in provider.requests[0].tools]
    assert len(names) == 8  # 全部内置工具（含 snip / search_history）


def test_agent_plan_narrow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = SkillManager(load_skills(tmp_path, tmp_path / "home"))
    manager.activate("test")  # bash, read_file, grep
    registry = new_default_registry()
    registry.register(SkillTool(manager))
    provider = FakeProvider(["ok"])
    engine, _ = new_engine(tempfile.mkdtemp())
    agent = Agent(provider, registry, "dev", engine, NullContext(), skills=manager)

    async def _go():
        async for _ in agent.run("hi", Conversation(), asyncio.Event(), Mode.PLAN):
            pass

    asyncio.run(_go())
    names = [t["name"] for t in provider.requests[0].tools]
    # PLAN 只读 + 白名单叠加：read_file/grep 只读，bash 非只读被 PLAN 排除，
    # load_skill 只读恒在。
    assert set(names) == {"read_file", "grep", LOAD_SKILL_TOOL}


def test_load_skill_survives_whitelist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = SkillManager(load_skills(tmp_path, tmp_path / "home"))
    manager.activate("commit")  # read_file, grep, bash
    registry = new_default_registry()
    registry.register(SkillTool(manager))
    provider = FakeProvider(["ok"])
    engine, _ = new_engine(tempfile.mkdtemp())
    agent = Agent(provider, registry, "dev", engine, NullContext(), skills=manager)
    _run(agent)
    names = [t["name"] for t in provider.requests[0].tools]
    assert LOAD_SKILL_TOOL in names  # 系统级工具不被收窄
    assert "write_file" not in names and "edit_file" not in names
