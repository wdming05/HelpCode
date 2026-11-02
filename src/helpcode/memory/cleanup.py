"""过期会话清理。

**判据是会话 ID 里编码的创建时刻，不是文件 mtime。**

mtime 不是「创建时间」：复制目录、备份还原、云同步、`touch` 都会重写它。用 mtime
判会让一批老会话永远不过期——用户每次打开项目，那些目录都被「刚刚访问过」，
于是一直留着。

代价必须说清：一个 30 天前创建、但昨天还在写的会话，按 ID 判定也算过期。
补偿是 ``keep`` 参数保护**本次恢复目标**，所以正在用的那个不会被删；更早的那些
即使近期翻看过也会被清掉。这是刻意的取舍：会话的留存价值与创建时间强相关，
与「最后一次被读」弱相关。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .constants import LEGACY_TOOL_RESULTS, SESSION_TTL_DAYS, SESSIONS_DIR
from .session import parse_session_time

logger = logging.getLogger(__name__)


@dataclass
class CleanupReport:
    sessions_removed: int = 0
    orphan_files_removed: int = 0
    warnings: list[str] = field(default_factory=list)

    def banner_line(self) -> str:
        """启动 banner 用的一行；什么都没删就返回空串（不占屏幕）。"""
        if not self.sessions_removed:
            return ""
        return f"已清理 {self.sessions_removed} 个过期会话"


def run(
    root: Path,
    *,
    now: datetime | None = None,
    keep: str | None = None,
) -> CleanupReport:
    """删除过期会话目录 + 旧全局落盘目录里的过期孤儿文件。

    ``keep`` 是本次要恢复的会话 ID，即使过期也不删——否则会出现「先清理、
    再恢复，结果恢复目标被删了」。
    """
    report = CleanupReport()
    now = now or datetime.now()
    cutoff = now - timedelta(days=SESSION_TTL_DAYS)

    base = root / SESSIONS_DIR
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name == keep:
                continue

            try:
                created = parse_session_time(entry.name)
            except ValueError:
                # 旧格式 / 手放目录：不删、不计数（D24）
                logger.debug("跳过无法解析的会话目录：%s", entry)
                continue

            if created > cutoff:
                continue

            try:
                shutil.rmtree(entry)
                report.sessions_removed += 1
            except OSError as exc:
                report.warnings.append(
                    f"{entry}: 删除失败（{exc.__class__.__name__}），已跳过"
                )

    _clean_legacy_tool_results(root, cutoff, report)
    return report


def _clean_legacy_tool_results(
    root: Path, cutoff: datetime, report: CleanupReport
) -> None:
    """清理 ch08 遗留的全局落盘目录。

    这里**只能**用 mtime——旧目录里没有 ID 可解析。这是本模块唯一读 mtime 的地方，
    注释写清是为了防止将来有人「统一改成 ID」时把这段改坏。
    """
    legacy = root / LEGACY_TOOL_RESULTS
    if not legacy.is_dir():
        return

    for path in sorted(legacy.iterdir()):
        if not path.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if mtime > cutoff:
                continue
            path.unlink()
            report.orphan_files_removed += 1
        except OSError as exc:
            report.warnings.append(
                f"{path}: 删除失败（{exc.__class__.__name__}），已跳过"
            )

    try:
        if not any(legacy.iterdir()):
            legacy.rmdir()
    except OSError as exc:
        report.warnings.append(f"{legacy}: 目录清理失败（{exc.__class__.__name__}）")


__all__ = ["CleanupReport", "run"]
