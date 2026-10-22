"""环境信息采集与渲染。"""

import asyncio
import datetime
import os
import platform
from dataclasses import dataclass


@dataclass
class Environment:
    """一次会话的环境信息（工作目录/平台/日期/git 状态/版本/模型）。"""

    working_dir: str = ""
    platform: str = ""
    date: str = ""
    git_status: str = ""
    version: str = ""
    model: str = ""

    def render(self) -> str:
        """渲染为「环境信息」文本段：逐行 Key: Value，空值项省略。"""
        items = [
            ("工作目录", self.working_dir),
            ("平台", self.platform),
            ("日期", self.date),
            ("Git 状态", self.git_status),
            ("版本", self.version),
            ("模型", self.model),
        ]
        return "\n".join(f"{key}: {value}" for key, value in items if value)


async def gather_environment(version: str, model: str) -> Environment:
    """采集一次会话的环境信息。不读取任何环境变量。"""
    try:
        working_dir = os.getcwd()
    except OSError:
        working_dir = ""
    env = Environment(
        working_dir=working_dir,
        platform=platform.system().lower(),
        date=datetime.date.today().isoformat(),
        version=version,
        model=model,
    )
    env.git_status = await _git_status()
    return env


async def _git_status() -> str:
    """采集 git 状态摘要；失败/非 git 目录/超时降级为空串。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        if proc.returncode != 0:
            return ""  # 非 git 目录
        text = stdout.decode("utf-8", errors="ignore").strip()
        if not text:
            return "clean"
        lines = text.splitlines()
        if len(lines) > 3:
            return f"{len(lines)} 个文件改动"
        return "; ".join(lines)
    except (FileNotFoundError, TimeoutError, OSError):
        return ""
