"""执行命令工具。"""

import os
import subprocess
from pathlib import Path

from . import DEFAULT_TIMEOUT, ToolResult, _truncate

# 命令输出截断上限（字符）
_OUTPUT_LIMIT = 4000


class BashTool:
    """在工作目录执行 shell 命令，带超时与输出截断。"""

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

    def run(
        self,
        command: str,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> ToolResult:
        # timeout 参数仅供测试注入极短超时；默认用 DEFAULT_TIMEOUT
        effective = timeout if timeout is not None else DEFAULT_TIMEOUT
        bash = self._find_bash()
        if bash:
            cmd = [bash, "-c", command]
            shell = False
        else:
            cmd = command
            shell = True

        try:
            result = subprocess.run(
                cmd,
                check=False,
                timeout=effective,
                capture_output=True,
                text=True,
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
