"""Agent Loop：多轮循环，自主调用工具直到满足停止条件。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from . import prompt
from .context import CompressInfo, ContextManager
from .conversation import Conversation
from .llm import (
    ContextLengthExceeded,
    Provider,
    Request,
    System,
    TokenUsage,
    ToolCall,
)
from .permission import (
    Decision,
    Engine,
    Mode,
    Outcome,
    check,
    friendly_name,
    persist_local_allow,
)
from .tool import AsyncTool, Registry, ToolResult

logger = logging.getLogger(__name__)


@dataclass
class PermissionRequest:
    """人在回路：待批准的工具调用（respond 是回填句柄）。"""

    tool_call: ToolCall
    friendly_name: str
    preview: str
    reason: str
    respond: asyncio.Future  # Future[Outcome]


@dataclass
class AgentEvent:
    """Agent 发给上层（TUI）的事件。"""

    text: str = ""  # 文本增量（流式）
    tool_start: ToolCall | None = None  # 工具调用开始（工具名 + 参数）
    tool_end: ToolResult | None = None  # 工具调用结束（结果摘要 + 是否错误）
    tool_call: ToolCall | None = None  # tool_end 对应的调用（配对）
    token_usage: TokenUsage | None = None  # 一轮结束时的用量
    iteration: int | None = None  # 当前迭代轮次（进度）
    notice: str | None = None  # 停止提示（非错误）
    permission_request: PermissionRequest | None = None  # 人在回路请求
    compress: CompressInfo | None = None  # 压缩事件（start / done，带 token）
    done: bool = False  # 本轮（一次 run）结束
    err: Exception | None = None  # LLM 流出错


# 每 N 轮自然结束提炼一次笔记。默认 1 = 每轮（ch09 spec 已确认的行为）；
# 改成 5 只是省调用次数，不改变任何断言。
MEMORY_UPDATE_EVERY = 1
# 命中这些词时**不看轮次**立即提炼：用户说「记住…」的当下信息量最高，
# 等到间隔到了再提炼，会被后续对话冲淡。
MEMORY_SIGNAL_WORDS = ("记住", "以后都", "别再", "下次要", "记一下")


class Agent:
    """多轮循环：输入 → 流式 → 权限检查 → 执行工具 → 回灌 → 循环。"""

    MAX_ITERATIONS = 25
    MAX_UNKNOWN_RUN = 3
    BATCH_TIMEOUT = 30.0
    PLAN_REMINDER_INTERVAL = 4

    def __init__(
        self,
        provider: Provider,
        registry: Registry,
        version: str,
        engine: Engine,
        context: ContextManager,
        system_extra: tuple[str, str] = ("", ""),
        on_turn_end=None,
        skills=None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self._version = version
        self.engine = engine
        # 故意不给默认值：忘记装配就 TypeError，而不是跑到一半才发现没压缩
        self.context = context
        # （指令, 记忆索引）的**快照**：在构造时定下，整个会话不变。
        # 若每轮重读，笔记一写文件 stable 就变，ch05 的 prompt 缓存整段失效。
        self._system_extra = system_extra
        # Skill 管理器（鸭子类型）：提供 render_available()/render_active()/
        # allowed_tools() 三个方法。测试不传即空行为，agent 不 import skill。
        self._skills = skills
        self._on_turn_end = on_turn_end
        self._note_tasks: set[asyncio.Task] = set()
        self._turn_count = 0

    async def run(
        self,
        user_input: str,
        conv: Conversation,
        cancel_event: asyncio.Event,
        mode: Mode,
    ) -> AsyncIterator[AgentEvent]:
        conv.add_user(user_input)
        unknown_run = 0

        # ch05：采集环境 + 装配稳定 system（一次，循环外）
        env = await prompt.gather_environment(self._version, self.provider.model)
        # ch11：已激活 Skill 每轮从管理器现读——激活状态会随 load_skill 变化，
        # 不能像指令/记忆那样在构造时快照。
        if self._skills is not None:
            active_skills = self._skills.render_active()
            available_skills = self._skills.render_available()
        else:
            active_skills = available_skills = ""
        sys = prompt.build_system_prompt(
            self._system_extra[0],
            self._system_extra[1],
            active_skills,
            available_skills,
        )
        env_text = env.render()

        # ch11：白名单收窄——有 Skill 激活时只露出白名单并集（恒含 load_skill）。
        allowed = self._skills.allowed_tools() if self._skills is not None else None

        for iteration in range(1, self.MAX_ITERATIONS + 1):
            # 终止优先级：取消最高（协作式，每轮开头检查）
            if cancel_event.is_set():
                yield AgentEvent(notice="已取消")
                self._finish_cancelled(conv)
                yield AgentEvent(done=True)
                return

            yield AgentEvent(iteration=iteration)

            # 按模式选工具与提醒
            if mode is Mode.PLAN:
                tools = self.registry.read_only_definitions()
                full = (iteration == 1) or (
                    (iteration - 1) % self.PLAN_REMINDER_INTERVAL == 0
                )
                reminder = prompt.plan_reminder(full)
            else:
                tools = self.registry.export()
                reminder = ""

            # 白名单软过滤：不删 registry（授权仍走权限引擎），只收窄模型可见工具
            if allowed is not None:
                tools = [t for t in tools if t["name"] in allowed]

            # ch08：恢复段要写当前工具集，所以先算出来
            tool_names = [t["name"] for t in (tools or [])]

            # ch08：每个请求前估算一次；需要压缩则先发 start 事件（用户立刻有反馈）
            if self.context.should_compress(conv):
                yield AgentEvent(
                    compress=CompressInfo(
                        phase="start", tokens_before=self.context.estimate(conv)
                    )
                )

            report = await self.context.before_request(conv, cancel_event, tool_names)
            if report.cancelled:
                yield AgentEvent(notice="已取消")
                yield AgentEvent(done=True)
                return
            if report.notice:
                yield AgentEvent(notice=report.notice)
            if report.info is not None:
                yield AgentEvent(compress=report.info)

            # 流式收集：双路（实时 yield + 累积完整文本）
            # 撞墙（PTL）则紧急压缩后在同一迭代重试一次，不消耗额外迭代预算
            out: dict = {}
            emergency_retried = False
            while True:
                out = {}
                hit_wall = False
                async for ev in self._stream_once(
                    conv, sys, env_text, tools, reminder, out
                ):
                    if ev.err is None:
                        yield ev
                        continue
                    # 三个终止条件顺序不能变：
                    # 非 PTL 立刻上报 → 已重试过就放弃 → 已吐过字不能重试（会重复）
                    if (
                        not isinstance(ev.err, ContextLengthExceeded)
                        or emergency_retried
                        or out.get("text")
                    ):
                        yield ev
                        return

                    emergency_retried = True
                    hit_wall = True
                    before = self.context.estimate(conv)
                    yield AgentEvent(
                        compress=CompressInfo(phase="start", tokens_before=before)
                    )

                    rep = await self.context.compact_now(
                        conv, emergency=True, tool_names=tool_names
                    )
                    after = rep.tokens_after or self.context.estimate(conv)
                    yield AgentEvent(
                        compress=CompressInfo(
                            phase="done",
                            tokens_before=before,
                            tokens_after=after,
                            summarized=rep.summarized,
                            kept=rep.kept,
                            spilled=rep.spilled,
                        )
                    )

                    # 压完仍贴着窗口 → 重试必然再撞，直接报错，不烧钱
                    if rep.failed or not self.context.has_room_after_compress(after):
                        yield ev
                        return
                    break
                if not hit_wall:
                    break

            text = out.get("text", "")
            tool_calls = out.get("tool_calls")
            usage = out.get("usage")

            # 正常完成：纯文本（无工具），或混合（文本 + 工具）时丢弃工具
            if not tool_calls or text:
                conv.add_assistant(text)
                self._maybe_start_note_task(conv)
                if usage is not None:
                    yield AgentEvent(token_usage=usage)
                yield AgentEvent(done=True)
                return

            # 纯工具调用：记历史
            conv.add_assistant_tool_calls(tool_calls)

            # 未知工具检测：该轮所有调用都未知 → streak+1，否则清零
            if all(self.registry.get(c.name) is None for c in tool_calls):
                unknown_run += 1
            else:
                unknown_run = 0
            if unknown_run >= self.MAX_UNKNOWN_RUN:
                yield AgentEvent(notice="连续请求未知工具，已停止")
                self._ensure_assistant_tail(conv, "连续请求未知工具，已停止")
                yield AgentEvent(done=True)
                return

            # 执行（批次内权限检查 + 并发/串行），结果按原始顺序回灌
            results: list[ToolResult] = [
                ToolResult(ok=False, content="(未执行)") for _ in tool_calls
            ]
            async for ev in self._execute_calls(
                tool_calls, mode, cancel_event, results
            ):
                yield ev
            for call in tool_calls:
                yield AgentEvent(tool_start=call)
            for call, result in zip(tool_calls, results):
                yield AgentEvent(tool_end=result, tool_call=call)
                conv.add_tool_result(call.id, result)

        # 迭代上限
        yield AgentEvent(notice="已达到最大迭代次数")
        self._ensure_assistant_tail(conv, "已达到最大迭代次数")
        yield AgentEvent(done=True)

    async def _stream_once(
        self,
        conv: Conversation,
        sys: str,
        env_text: str,
        tools: list[dict] | None,
        reminder: str,
        out: dict,
    ) -> AsyncIterator[AgentEvent]:
        """组装 Request 并消费一轮流；yield 事件，收集结果写入 out。"""
        req = Request(
            messages=conv.messages(),
            tools=tools,
            system=System(stable=sys, environment=env_text),
            reminder=reminder,
        )
        text = ""
        tool_calls = None
        usage = None
        async for ev in self.provider.stream(req):
            if ev.err is not None:
                # ★ 出错也要交出已累积的文本：上层靠它判断「是否已经吐过字」，
                #   吐过就不能重试（重试会让同一段话出现两次）。
                out["text"] = text
                yield AgentEvent(err=ev.err)
                return
            if ev.text:
                text += ev.text
                yield AgentEvent(text=ev.text)
            if ev.usage:
                usage = ev.usage
            if ev.done:
                tool_calls = ev.tool_calls
                break
        out["text"] = text
        out["tool_calls"] = tool_calls
        out["usage"] = usage
        # ch08：用服务端实测值重建锚点（含 cache_read/cache_write，见 D10）
        if usage is not None:
            self.context.after_stream(usage, len(conv.messages()))

    async def compact_now(self, conv: Conversation, emergency: bool = False):
        """手动 / 紧急压缩（TUI 的 /compact 与撞墙重试都走这里）。"""
        return await self.context.compact_now(
            conv, emergency=emergency, tool_names=self.registry.names()
        )

    async def _execute_calls(
        self,
        calls: list[ToolCall],
        mode: Mode,
        cancel_event: asyncio.Event,
        results: list[ToolResult],
    ) -> AsyncIterator[AgentEvent]:
        """批次循环内权限检查 + 并发/串行执行；yield permission_request。"""
        i = 0
        while i < len(calls):
            name = calls[i].name
            # 未知工具分层：registry 不存在 → 直接报错，不进权限流水线
            if self.registry.get(name) is None:
                results[i] = ToolResult(
                    ok=False, content=f"未知工具: {name}", is_error=True
                )
                i += 1
                continue

            if self.registry.is_read_only(name):
                # 连续只读段：check（Deny 预置不进 gather，Allow 并发）
                batch = [i]
                j = i + 1
                while j < len(calls) and self.registry.is_read_only(calls[j].name):
                    batch.append(j)
                    j += 1
                to_run: list[int] = []
                for k in batch:
                    decision, reason = check(self.engine, mode, calls[k], True)
                    if decision is Decision.DENY:
                        results[k] = ToolResult(ok=False, content=reason, is_error=True)
                    else:  # 只读永不 Ask
                        to_run.append(k)
                try:
                    gathered = await asyncio.wait_for(
                        asyncio.gather(*[self._execute_one(calls[k]) for k in to_run]),
                        timeout=self.BATCH_TIMEOUT,
                    )
                except TimeoutError:
                    gathered = [
                        ToolResult(
                            ok=False,
                            content=f"错误：工具批执行超时（>{self.BATCH_TIMEOUT}s）",
                            is_error=True,
                        )
                        for _ in to_run
                    ]
                for k, r in zip(to_run, gathered):
                    results[k] = r
                i = j
            else:
                # 副作用串行：check → ALLOW 执行 / DENY 预置 / ASK 人在回路
                decision, reason = check(self.engine, mode, calls[i], False)
                if decision is Decision.DENY:
                    results[i] = ToolResult(ok=False, content=reason, is_error=True)
                elif decision is Decision.ALLOW:
                    results[i] = await self._execute_one(calls[i])
                else:  # ASK
                    out: dict = {}
                    async for ev in self._request_approval(
                        calls[i], reason, cancel_event, out
                    ):
                        yield ev
                    if out.get("cancelled"):
                        results[i] = ToolResult(
                            ok=False, content="已取消", is_error=True
                        )
                        return  # 取消：停止后续执行，交上层收尾
                    outcome = out.get("outcome")
                    if outcome is Outcome.ALLOW_FOREVER:
                        try:
                            persist_local_allow(self.engine, calls[i])
                        except Exception:
                            logger.warning("持久化永久放行规则失败", exc_info=True)
                        results[i] = await self._execute_one(calls[i])
                    elif outcome is Outcome.ALLOW_ONCE:
                        results[i] = await self._execute_one(calls[i])
                    else:  # DENY_ONCE
                        results[i] = ToolResult(
                            ok=False, content="用户拒绝该操作", is_error=True
                        )
                i += 1

    async def _request_approval(
        self,
        call: ToolCall,
        reason: str,
        cancel_event: asyncio.Event,
        out: dict,
    ) -> AsyncIterator[AgentEvent]:
        """人在回路：yield permission_request，结果写入 out（outcome/cancelled）。"""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        req = PermissionRequest(
            tool_call=call,
            friendly_name=friendly_name(call.name),
            preview=_preview_args(call.input),
            reason=reason,
            respond=future,
        )
        yield AgentEvent(permission_request=req)
        _done, pending = await asyncio.wait(
            [asyncio.ensure_future(future), asyncio.create_task(cancel_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if cancel_event.is_set():
            out["cancelled"] = True
            return
        out["outcome"] = future.result()

    async def _execute_one(self, call: ToolCall) -> ToolResult:
        """执行一个工具调用；未知工具/异常均包成 ToolResult。"""
        tool = self.registry.get(call.name)
        if tool is None:
            return ToolResult(
                ok=False, content=f"错误：未知工具 {call.name}", is_error=True
            )
        try:
            # 异步工具（MCP 等）直接在事件循环里跑：它们的会话绑定 loop，
            # 丢进线程池调用必然失败。
            if isinstance(tool, AsyncTool):
                return await tool.arun(**call.input)
            # 同步工具函数放到线程执行，避免阻塞事件循环
            return await asyncio.to_thread(tool.run, **call.input)
        except Exception as e:
            return ToolResult(
                ok=False, content=f"错误：工具执行失败 {e}", is_error=True
            )

    # ------------------------------------------------------------ 自动笔记

    def _should_update_notes(self, conv: Conversation) -> bool:
        """这一轮要不要触发提炼。

        判定顺序：信号词（立即）→ 每轮 → 按间隔取模。
        """
        if self._on_turn_end is None:
            return False

        last_user = next(
            (m for m in reversed(conv.messages()) if m.role == "user"), None
        )
        if last_user is not None and any(
            word in last_user.content for word in MEMORY_SIGNAL_WORDS
        ):
            return True

        if MEMORY_UPDATE_EVERY <= 1:
            return True
        return self._turn_count % MEMORY_UPDATE_EVERY == 0

    def _maybe_start_note_task(self, conv: Conversation) -> None:
        """异步起一个提炼任务，**不 await**：笔记慢不该让用户等。

        任务引用存进集合，退出时由 ``drain_note_tasks`` 统一等待——否则
        进程退出会把写了一半的笔记丢掉，还会留下 asyncio 的清理告警。
        """
        self._turn_count += 1
        if not self._should_update_notes(conv):
            return

        task = asyncio.create_task(self._on_turn_end(conv))
        self._note_tasks.add(task)
        task.add_done_callback(self._note_tasks.discard)

    async def drain_note_tasks(self, timeout: float = 2.0) -> None:
        """退出前等沉淀任务收尾；超时就放弃（笔记不值得拖住退出）。"""
        pending = [t for t in self._note_tasks if not t.done()]
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout
            )
        except TimeoutError:
            # Python 3.11+ 里 asyncio.TimeoutError 就是内建 TimeoutError
            pass

    def _ensure_assistant_tail(self, conv: Conversation, text: str) -> None:
        """确保历史以 assistant 消息结尾（终止时的统一收尾，防 400）。"""
        if conv.last_role() != "assistant":
            conv.add_assistant(text)

    def _finish_cancelled(self, conv: Conversation) -> None:
        """取消收尾：为「已发起但未回灌」的工具调用补「已取消」，保证配对合法。"""
        msgs = conv.messages()
        if msgs and msgs[-1].role == "assistant" and msgs[-1].tool_calls:
            for tc in msgs[-1].tool_calls:
                conv.add_tool_result(
                    tc.id, ToolResult(ok=False, content="已取消", is_error=True)
                )
        self._ensure_assistant_tail(conv, "已取消")


def _preview_args(input: dict) -> str:
    """提取关键参数做简要预览。"""
    for key in ("path", "command", "pattern"):
        if key in input:
            s = str(input[key])
            return s if len(s) <= 40 else s[:40] + "…"
    return ""
