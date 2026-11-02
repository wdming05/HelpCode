"""窗格后端的成员进程入口。

窗格里的队员是**独立进程**：它得自己装配 provider、权限引擎、工具与工作目录，
不能借主进程的任何对象。跑完一次任务就退出——常驻监听不在本章范围，
Lead 之后的指派会重新拉起一个进程并读 session 存档续上下文。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .. import __version__
from ..agent import Agent
from ..config import ConfigError, load
from ..context import ContextManager
from ..conversation import Conversation
from ..llm import new_provider
from ..memory.session import SessionWriter
from ..permission import Mode, new_engine
from ..tool import new_default_registry
from .mailbox import Mailbox, Message
from .model import Team


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="helpcode.team.worker")
    parser.add_argument("--team", required=True, help="团队目录名（sanitized）")
    parser.add_argument("--member", required=True, help="成员名")
    parser.add_argument("--worktree", required=True, help="工作目录（绝对路径）")
    parser.add_argument("--session-dir", required=True, help="会话存档目录")
    parser.add_argument("--prompt-file", required=True, help="任务文本文件")
    parser.add_argument("--model", default="", help="模型覆盖")
    parser.add_argument("--plan-mode", action="store_true", help="以计划模式起步")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    prompt_path = Path(args.prompt_file)
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"读取任务文件失败：{e}", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_run(args, prompt))
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # 成员失败不该静默
        print(f"队员执行失败：{e.__class__.__name__}: {e}", file=sys.stderr)
        return 1


async def _run(args: argparse.Namespace, prompt: str) -> int:
    root = Path(args.worktree)
    teams_root = Path.home() / ".helpcode" / "teams"
    config_dir = teams_root / args.team
    try:
        team = Team.load(config_dir / "config.json")
    except Exception as e:
        print(f"团队配置不可用：{e}", file=sys.stderr)
        return 1

    try:
        cfg = load(str(root / ".helpcode" / "config.yaml"))
    except ConfigError as e:
        print(f"缺少可用的模型配置：{e}", file=sys.stderr)
        return 1
    if not cfg.providers:
        print("配置里没有任何 provider", file=sys.stderr)
        return 1

    provider_cfg = cfg.providers[0]
    if args.model:
        from dataclasses import replace

        provider_cfg = replace(provider_cfg, model=args.model)
    provider = new_provider(provider_cfg)
    engine, _ = new_engine(str(root))

    session_dir = Path(args.session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    writer = SessionWriter(session_dir / "conversation.jsonl", provider_cfg.model)
    conv = Conversation(on_append=writer.append)

    context = ContextManager(
        provider,
        root,
        provider_cfg.context_window or 128000,
        spill_subdir=session_dir / "tool-results",
    )
    registry = new_default_registry()
    # 队员与 Lead 共享同一套沙箱语义（默认关，由 /sandbox 控制）
    registry.attach_sandbox(engine.sandbox)
    agent = Agent(
        provider,
        registry,
        __version__,
        engine,
        context,
        cwd=str(root),
    )

    mode = Mode.PLAN if args.plan_mode else Mode.DEFAULT
    result = await agent.run_to_completion(conv, prompt, mode)

    # 收尾：标记空闲 + 通知 Lead（主进程不在身边，直接写文件）
    box = Mailbox(team.mailbox_root)
    try:
        team.set_member_active(args.member, False)
    except Exception as e:  # 花名册写不进去也要把空闲通知发出去
        print(f"更新成员状态失败：{e}", file=sys.stderr)
    box.send(
        "lead",
        Message(
            sender=args.member,
            content=f"{args.member} 已停下：{result[:200]}",
            type="idle_notification",
            summary=f"{args.member} 空闲",
        ),
    )
    if args.plan_mode:
        box.send(
            "lead",
            Message(
                sender=args.member,
                content=result or "（计划为空）",
                type="plan_approval_request",
                request_id=f"plan-{args.member}",
                summary="计划待审批",
            ),
        )
    writer.close()
    print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
