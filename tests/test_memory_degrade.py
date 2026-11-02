"""笔记编排与降级：失败不影响对话、并发不丢笔记。"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest

from helpcode.llm import Message, Request, StreamEvent, TokenUsage
from helpcode.memory import notes

FIXED = datetime(2026, 10, 24, 12, 0, 0)


def _payload(title: str, body: str = "正文") -> str:
    return json.dumps(
        [
            {
                "action": "add",
                "type": "project_knowledge",
                "target": "project",
                "title": title,
                "description": "说明",
                "body": body,
            }
        ],
        ensure_ascii=False,
    )


class _Provider:
    """最小 provider：按脚本返回文本，可注入延迟与错误。"""

    def __init__(self, script: list, delay: float = 0.0) -> None:
        self._script = list(script)
        self._delay = delay
        self.requests: list[Request] = []
        self.call_count = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        self.requests.append(req)
        self.call_count += 1
        item = self._script.pop(0) if self._script else ""
        if self._delay:
            await asyncio.sleep(self._delay)
        if isinstance(item, Exception):
            yield StreamEvent(err=item)
            return
        yield StreamEvent(text=str(item))
        yield StreamEvent(usage=TokenUsage(input_tokens=10, output_tokens=5))
        yield StreamEvent(done=True)


def _chunk(text: str = "记住：提交前跑 ruff") -> list[Message]:
    return [
        Message(role="user", content="以后提交前都跑 ruff"),
        Message(role="assistant", content=text),
    ]


def _stores(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "home_mem", tmp_path / "proj_mem"


def test_update_writes_notes(tmp_path):
    """端到端：调 provider → 解析 → 落文件 + 更新索引。"""
    home, proj = _stores(tmp_path)
    provider = _Provider([_payload("提交前跑 ruff")])

    updates = asyncio.run(notes.update(provider, _chunk(), "", home, proj, now=FIXED))

    assert len(updates) == 1
    assert (proj / "提交前跑-ruff.md").exists()
    assert "提交前跑 ruff" in (proj / "index.md").read_text(encoding="utf-8")


def test_update_request_is_isolated(tmp_path):
    """笔记请求 tools=None、system 是笔记专用提示（AC24）。"""
    home, proj = _stores(tmp_path)
    provider = _Provider([_payload("x")])

    asyncio.run(notes.update(provider, _chunk(), "- 旧", home, proj, now=FIXED))

    req = provider.requests[0]
    assert req.tools is None
    assert "- 旧" in req.system.stable


def test_update_returns_empty_on_provider_error(tmp_path):
    """provider 报错 → 返回空列表、不抛、不写任何文件（AC30）。"""
    home, proj = _stores(tmp_path)
    provider = _Provider([RuntimeError("upstream 500")])

    updates = asyncio.run(notes.update(provider, _chunk(), "", home, proj, now=FIXED))

    assert updates == []
    assert not (proj / "index.md").exists()


def test_update_returns_empty_on_unparseable(tmp_path):
    """模型胡言乱语 → 空列表，不产生笔记文件。"""
    home, proj = _stores(tmp_path)
    provider = _Provider(["我不知道该记什么"])

    assert asyncio.run(notes.update(provider, _chunk(), "", home, proj)) == []
    assert not (proj / "index.md").exists()


def test_update_does_not_modify_chunk(tmp_path):
    """提炼只读 chunk，不改主历史里的对象（N7）。"""
    home, proj = _stores(tmp_path)
    chunk = _chunk()
    before = [(m.role, m.content) for m in chunk]
    provider = _Provider([_payload("x")])

    asyncio.run(notes.update(provider, chunk, "", home, proj, now=FIXED))

    assert [(m.role, m.content) for m in chunk] == before


def test_concurrent_updates_keep_both(tmp_path):
    """两个更新并发跑 → 两条笔记都在索引里。

    **不加锁时这条必失败**：两个任务都读到同一份空索引，各自写回，
    后写的把先写的覆盖掉，索引只剩一行。
    """
    home, proj = _stores(tmp_path)
    provider = _Provider(
        [_payload("笔记甲"), _payload("笔记乙")],
        delay=0.05,  # 制造交错窗口
    )

    async def run_two():
        return await asyncio.gather(
            notes.update(provider, _chunk(), "", home, proj, now=FIXED),
            notes.update(provider, _chunk(), "", home, proj, now=FIXED),
        )

    results = asyncio.run(run_two())

    assert all(len(r) == 1 for r in results)
    lines = (proj / "index.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert any("笔记甲" in ln for ln in lines)
    assert any("笔记乙" in ln for ln in lines)


def test_tool_call_events_ignored(tmp_path):
    """流里混进 tool_calls 也不该被当成笔记内容。"""
    home, proj = _stores(tmp_path)

    class _Toolish(_Provider):
        async def stream(self, req):
            self.requests.append(req)
            self.call_count += 1
            yield StreamEvent(tool_calls=[])
            yield StreamEvent(text=_payload("正常笔记"))
            yield StreamEvent(done=True)

    provider = _Toolish([])
    updates = asyncio.run(notes.update(provider, _chunk(), "", home, proj, now=FIXED))

    assert len(updates) == 1
    assert (proj / "正常笔记.md").exists()


def test_lock_is_module_level():
    """锁是模块级单例：多个调用方共享同一把。"""
    assert isinstance(notes._lock, asyncio.Lock)


@pytest.mark.parametrize("target", ["user", "project"])
def test_target_routes_to_correct_store(tmp_path, target):
    home, proj = _stores(tmp_path)
    item = _payload("跨项目偏好")
    parsed = json.loads(item)
    parsed[0]["target"] = target
    provider = _Provider([json.dumps(parsed, ensure_ascii=False)])

    asyncio.run(notes.update(provider, _chunk(), "", home, proj, now=FIXED))

    expected = (home if target == "user" else proj) / "跨项目偏好.md"
    assert expected.exists()
