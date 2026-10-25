"""入口：加载配置与记忆、恢复会话、连接 MCP、启动 TUI。"""

import asyncio
import sys
from pathlib import Path

from .command import DuplicateCommandError, build_default_registry
from .config import Config, ConfigError, load
from .hook import HookEngine, load_hooks
from .mcp import McpManager, load_mcp_config
from .memory import cleanup as cleanup_mod
from .memory import index as index_mod
from .memory import instructions as instructions_mod
from .memory import session as session_mod
from .memory.restore import restore as restore_session
from .skill import (
    LOAD_SKILL_TOOL,
    SkillManager,
    WhitelistError,
    load_skills,
    register_skill_commands,
    validate_whitelists,
)
from .subagent import load_catalog
from .tool import new_default_registry
from .tui.app import HelpCodeApp

CONFIG_PATH = ".helpcode/config.yaml"


def main() -> None:
    """装配与启动 HelpCode。"""
    try:
        cfg = load(CONFIG_PATH)
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        sys.exit(1)

    # 命令注册表在启动期构造；别名冲突在这里就终止，不等到运行时。
    try:
        build_default_registry()
    except DuplicateCommandError as e:
        print(f"命令注册失败: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        # Ctrl+C 兜底（Textual 通常已处理）
        pass
    except Exception as e:
        print(f"启动失败: {e}", file=sys.stderr)
        sys.exit(1)


async def _run(cfg: Config) -> None:
    """同一个事件循环里：读记忆 → 恢复会话 → 连 MCP → 跑 TUI → 统一关闭。

    MCP 会话绑定建立它的事件循环，所以不能在外层 ``asyncio.run`` 连完再
    让 ``app.run()`` 另起一个循环——那样 session 一进 TUI 就失效。
    """
    root = Path.cwd()
    home = Path.home()

    # 1) 先扫描（清理要用它做 keep，恢复也要用它选目标）——只扫一次
    sessions = session_mod.scan(root)
    latest = sessions[0] if sessions else None

    # 2) 清理过期会话（keep 保护本次要恢复的那个）
    cleanup_report = cleanup_mod.run(root, keep=latest.session_id if latest else None)

    # 3) 指令与记忆索引：都是**读一次**，之后整个会话用这份快照
    instruction_set = instructions_mod.load(root, home)
    for warning in instruction_set.warnings:
        print(f"（记忆提示）{warning}", file=sys.stderr)

    bundle = index_mod.load(
        home / ".helpcode" / "memory", root / ".helpcode" / "memory"
    )
    for warning in bundle.warnings:
        print(f"（记忆提示）{warning}", file=sys.stderr)

    # 4) 恢复最近会话；没有就是新会话
    conv, writer, session_id, restored = _build_conversation(root, latest)

    manager = McpManager(load_mcp_config(root))
    mcp_tools = await manager.connect_all()
    try:
        # 5) ch11：加载 Skill + 校验白名单 + 装配命令（连 MCP 后才能知道全部工具名）
        skill_registry = load_skills(root, home)
        for warning in skill_registry.warnings:
            print(f"（Skill 提示）{warning}", file=sys.stderr)
        try:
            validate_whitelists(
                skill_registry,
                set(new_default_registry().names())
                | {t.name for t in mcp_tools}
                | {LOAD_SKILL_TOOL},
            )
        except WhitelistError as e:
            print(f"Skill 配置错误: {e}", file=sys.stderr)
            sys.exit(1)

        skill_manager = SkillManager(
            skill_registry, reload_fn=lambda: load_skills(root, home)
        )
        commands = build_default_registry()
        register_skill_commands(commands, skill_manager)

        # 6) ch12：加载 Hook（两层叠加），配置错误只告警不阻断
        hook_list, hook_warnings = load_hooks(root, home)
        for warning in hook_warnings:
            print(f"（Hook 提示）{warning}", file=sys.stderr)
        hook_engine = HookEngine(hook_list)

        # 7) ch13：加载角色目录（项目 > 用户 > 内置）
        catalog, catalog_warnings = load_catalog(root, home)
        for warning in catalog_warnings:
            print(f"（Agent 提示）{warning}", file=sys.stderr)

        app = HelpCodeApp(
            cfg.providers,
            mcp_tools,
            instructions=instruction_set.render(),
            memory=index_mod.render(bundle),
            conv=conv,
            writer=writer,
            restore=restored,
            cleanup=cleanup_report,
            sessions=sessions,
            session_id=session_id,
            commands=commands,
            instruction_set=instruction_set,
            index_bundle=bundle,
            skill_manager=skill_manager,
            hooks=hook_engine,
            catalog=catalog,
        )
        await app.run_async()
    finally:
        if writer is not None:
            writer.close()
        await manager.aclose()


def _build_conversation(root: Path, latest):
    """有最近会话就恢复，否则新建一个。

    **必须用 ``Conversation.from_messages``**：用 ``replace_messages`` 灌历史
    会立刻触发 ``on_replace``，在恢复的当下写一行 compact 标记 + 整份历史，
    等于把「恢复」记成了一次「压缩」。
    """
    from .conversation import Conversation
    from .memory.session import SessionWriter

    if latest is not None:
        report = restore_session(latest.jsonl)
        # 「未知」是扫描的展示占位，不是模型名。当成模型传下去会被写进后续每一行。
        known = latest.model if latest.model != session_mod.UNKNOWN_MODEL else ""
        writer = SessionWriter(latest.jsonl, known)
        conv = Conversation.from_messages(
            report.messages,
            on_append=writer.append,
            on_replace=_writer_replace(writer),
        )
        return conv, writer, latest.session_id, report

    session_id = session_mod.new_session_id()
    try:
        session_mod.ensure_session_dir(root, session_id)
    except OSError as exc:
        print(f"（存档提示）无法创建会话目录：{exc}", file=sys.stderr)
        return Conversation(), None, None, None

    writer = SessionWriter(session_mod.jsonl_path(root, session_id), "")
    conv = Conversation(
        on_append=writer.append,
        on_replace=_writer_replace(writer),
    )
    return conv, writer, session_id, None


def _writer_replace(writer):
    """压缩时：先落标记、再逐条追加（顺序不能反）。"""

    def _replace(messages) -> None:
        writer.write_compact_marker()
        writer.append_all(messages)

    return _replace
