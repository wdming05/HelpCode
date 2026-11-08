"""执行命令工具。"""

import os
import subprocess
from pathlib import Path

from ..permission.os_sandbox import SandboxState
from . import DEFAULT_TIMEOUT, ToolResult, _truncate

# 命令输出截断上限（字符）
_OUTPUT_LIMIT = 4000


class BashTool:
    """在工作目录执行 shell 命令，带超时与输出截断。

    ``sandbox`` 由装配层注入（``Registry.attach_sandbox``），和
    ``PermissionEngine`` 共享同一份状态——``/sandbox`` 切换时两边同时生效。
    """

    name = "bash"
    read_only = False
    cwd_aware = True  # ch14：支持显式工作目录
    description = (
        "执行一条 shell 命令，返回退出码、标准输出与标准错误。"
        "命令有超时限制；输出过长会被截断。"
        "读文件、找文件、搜内容请优先用 read_file/glob/grep，不要用 bash 拼凑。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
        },
        "required": ["command"],
    }

    def __init__(self, sandbox: SandboxState | None = None) -> None:
        self._sandbox = sandbox

    def attach_sandbox(self, sandbox: SandboxState | None) -> None:
        """装配层在拿到权限引擎后调用（引擎里才有建好的沙箱状态）。"""
        self._sandbox = sandbox

    def run(
        self,
        command: str,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> ToolResult:
        # timeout 参数仅供测试注入极短超时；默认用 DEFAULT_TIMEOUT
        effective = timeout if timeout is not None else DEFAULT_TIMEOUT
        bash = self._find_bash()

        # 三种形态：显式 bash argv / 显式 sh argv（沙箱要求）/ 交给 shell 的字符串
        use_sandbox = self._sandbox is not None and self._sandbox.active()
        if bash:
            argv: list[str] | str = [bash, "-c", command]
            shell = False
        elif use_sandbox:
            # 沙箱只能包装 argv，不能包装 shell 字符串——这里显式指定 /bin/sh
            argv = ["/bin/sh", "-c", command]
            shell = False
        else:
            argv = command
            shell = True

        if use_sandbox:
            wrapped = self._wrap(
                argv if isinstance(argv, list) else ["/bin/sh", "-c", argv]
            )
            if wrapped is None:
                # 包装失败就**不执行**：用户开沙箱是要隔离，静默降级等于骗人
                return ToolResult(
                    ok=False,
                    content=(
                        "错误：OS 沙箱包装失败，命令未执行。"
                        "请检查 /sandbox 状态或关闭沙箱后重试。"
                    ),
                )
            argv = wrapped
            shell = False

        try:
            result = subprocess.run(
                argv,
                check=False,
                timeout=effective,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",  # Windows 命令输出可能混 GBK 字节，严格 utf-8 会让 reader 线程崩掉
                shell=shell,
                cwd=cwd or None,  # None = 沿用进程工作目录（与改造前一致）
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                ok=False, content=f"错误：命令超时（>{effective}s）：{command}"
            )
        except Exception as e:
            return ToolResult(ok=False, content=f"错误：执行命令失败 {e}")

        parts = [f"退出码：{result.returncode}"]
        if result.stdout:
            parts.append(f"标准输出：\n{_truncate(result.stdout, _OUTPUT_LIMIT)}")
        if result.stderr:
            parts.append(f"错误输出：\n{_truncate(result.stderr, _OUTPUT_LIMIT)}")
        return ToolResult(ok=result.returncode == 0, content="\n".join(parts))

    def _wrap(self, argv: list[str]) -> list[str] | None:
        """把 argv 交给沙箱后端包装；后端异常时返回 None（调用方据此拒绝执行）。"""
        assert self._sandbox is not None and self._sandbox.backend is not None
        try:
            return self._sandbox.backend.wrap(argv, self._sandbox.config)
        except Exception:
            # 任何后端异常都按「隔离不可用」处理：宁可拒绝执行，也不能假装被保护
            return None

    def _find_bash(self) -> str | None:
        """在 PATH 里找一个不是 WSL 启动器的真 bash（Git Bash）。"""
        for dir_ in os.environ.get("PATH", "").split(os.pathsep):
            candidate = Path(dir_) / "bash.exe"
            if not candidate.is_file():
                continue
            lowered = str(candidate).lower()
            if "system32" in lowered or "windowsapps" in lowered:
                continue  # 跳过 WSL 启动器
            return str(candidate)
        return None
