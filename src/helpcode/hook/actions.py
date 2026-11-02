"""四种 Hook 动作的实现。

全部包在 try/except 里：Hook 自身失败只记日志，绝不向上抛（N1）。
唯一能「影响主流程」的通道是拦截事件下的 shell ``exit 2``（F13/F16）。
"""

from __future__ import annotations

import asyncio
import json
import locale
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

from .events import INTERCEPT_EVENTS
from .schema import (
    COMMAND,
    HTTP,
    PROMPT,
    SUBAGENT,
    Action,
    Hook,
    HookResult,
    field_to_text,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def subst(text: str, payload: dict) -> str:
    """把 ``{{a.b}}`` 替换成 payload 里的点路径值。"""
    return _PLACEHOLDER.sub(lambda m: field_to_text(payload, m.group(1)), text)


def _decode(data: bytes) -> str:
    """解码子进程 stderr。

    Windows 上 shell 的 echo 按**控制台 OEM 代码页**（中文系统是 cp936/GBK）输出，
    而不是 UTF-8；而 Python 可能运行在 UTF-8 模式（``PYTHONUTF8=1``）——
    此时 ``locale.getpreferredencoding()`` 也是 utf-8，直接用它解会乱码。
    所以显式查询 ``GetOEMCP()`` 并把 cp936/gbk 放进候选。
    """
    encs = ["utf-8"]
    if os.name == "nt":
        try:
            import ctypes

            oem = ctypes.windll.kernel32.GetOEMCP()
            encs.append(f"cp{oem}")
        except Exception:
            logger.debug("查询 OEM 代码页失败，退回通用候选", exc_info=True)
        encs += ["gbk", "cp936", "cp1252"]
    else:
        encs.append(locale.getpreferredencoding(False))
    for enc in encs:
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


async def run_action(hook: Hook, payload: dict) -> HookResult:
    """执行一条 Hook 的动作；任何异常都吞掉并记日志。"""
    try:
        return await _dispatch(hook.action, hook, payload)
    except Exception:
        logger.warning("Hook「%s」执行失败（已忽略）", hook.name, exc_info=True)
        return HookResult()


async def _dispatch(action: Action, hook: Hook, payload: dict) -> HookResult:
    if action.type == COMMAND:
        return await _run_command(hook, payload)
    if action.type == PROMPT:
        return HookResult(injected=[subst(action.prompt, payload)])
    if action.type == HTTP:
        return await _run_http(hook, payload)
    if action.type == SUBAGENT:
        # 子 Agent 本期占位：只记日志，等 SubAgent 章节对接
        logger.info("Hook「%s」的 subagent 动作尚未实现（占位）", hook.name)
        return HookResult()
    return HookResult()


async def _run_command(hook: Hook, payload: dict) -> HookResult:
    command = subst(hook.action.command, payload)
    env = dict(os.environ)
    env["HOOK_PAYLOAD"] = json.dumps(payload, ensure_ascii=False, default=str)
    env["HOOK_EVENT"] = hook.event.value

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError:
        logger.warning("Hook「%s」启动命令失败", hook.name, exc_info=True)
        return HookResult()

    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=hook.timeout)
    except TimeoutError:
        await _kill_tree(proc)
        logger.warning("Hook「%s」命令超时（%ss），已放弃", hook.name, hook.timeout)
        return HookResult()

    code = proc.returncode
    if code == 2 and hook.event in INTERCEPT_EVENTS:
        reason = _decode(err).strip() or "被 Hook 拦截"
        return HookResult(block_reason=reason)
    if code != 0:
        logger.warning("Hook「%s」命令退出码 %s（不拦截）", hook.name, code)
    return HookResult()


async def _kill_tree(proc) -> None:
    """超时后杀掉整个进程树。

    Windows 上 ``create_subprocess_shell`` 起的是 ``cmd.exe /c <命令>``，
    只 ``kill()`` cmd 会留下孙进程继续占着管道，``wait()`` 反而要等到孙进程
    自己结束（实测 0.3s 超时会拖到 5s+）。所以用 ``taskkill /T`` 连树一起杀。
    POSIX 上 ``sh -c`` 通常直接 exec 单条命令，``kill()`` 就够。
    """
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except OSError:
            proc.kill()
    else:
        proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except TimeoutError:
        pass


async def _run_http(hook: Hook, payload: dict) -> HookResult:
    action = hook.action
    data: Any = None
    if action.body is not None:
        data = json.dumps(action.body, ensure_ascii=False).encode("utf-8")
    else:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if action.headers:
        headers.update({str(k): str(v) for k, v in action.headers.items()})

    def _send() -> None:
        req = urllib.request.Request(
            action.url, data=data, headers=headers, method=action.method
        )
        with urllib.request.urlopen(req, timeout=hook.timeout):
            pass

    try:
        await asyncio.to_thread(_send)
    except (urllib.error.URLError, OSError, ValueError):
        logger.warning("Hook「%s」HTTP 请求失败（不拦截）", hook.name, exc_info=True)
    return HookResult()
