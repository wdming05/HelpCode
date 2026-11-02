"""团队管理：团队表的唯一拥有者，负责落盘与扫描。"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..worktree import Manager as WorktreeManager
from .backend import Backend, BackendType, detect_backend, make_backend
from .model import (
    Team,
    TeamError,
    TeamHasActiveMembersError,
    TeammateInfo,
    TeamNotFoundError,
    sanitize_name,
)

logger = logging.getLogger(__name__)

TEAMS_DIR = Path(".helpcode") / "teams"


class Manager:
    """一个进程内的团队表。

    ``registry`` 是留给「name → agent_id」独立注册表的扩展位：本章直接用
    花名册当映射（成员名本身唯一），不额外引一张表。
    """

    def __init__(
        self,
        home: Path,
        wt_mgr: WorktreeManager,
        task_mgr=None,
        registry=None,
    ) -> None:
        self.home = Path(home)
        self.teams_root = self.home / TEAMS_DIR
        self.wt_mgr = wt_mgr
        self.task_mgr = task_mgr
        self.registry = registry
        try:
            self.teams_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise TeamError(f"无法创建团队目录 {self.teams_root}：{e}") from e
        self.teams: dict[str, Team] = {}
        self._scan()

    # ------------------------------------------------------------ 查询

    def list(self) -> list[Team]:
        return sorted(self.teams.values(), key=lambda t: t.created_at)

    def get(self, name: str) -> Team | None:
        if not name:
            return None
        found = self.teams.get(name)
        if found is not None:
            return found
        try:
            return self.teams.get(sanitize_name(name))
        except TeamError:
            return None

    def backend_for(self, team: Team) -> Backend:
        return make_backend(BackendType(team.backend))

    # ------------------------------------------------------------ 创建

    def create(
        self,
        name: str,
        description: str = "",
        *,
        backend: BackendType | None = None,
        lead_id: str = "lead",
    ) -> Team:
        base = sanitize_name(name)
        sanitized = self._unique(base)
        config_dir = self.teams_root / sanitized
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise TeamError(f"无法创建团队目录 {config_dir}：{e}") from e

        kind = backend or detect_backend()
        team = Team(
            name=name.strip(),
            sanitized_name=sanitized,
            description=description.strip(),
            lead_agent_id=lead_id,
            config_dir=config_dir,
            backend=kind.value,
            members=[
                TeammateInfo(
                    name="lead",
                    agent_id=lead_id,
                    agent_type="lead",
                    is_active=None,
                )
            ],
        )
        team.save()
        self.teams[sanitized] = team
        return team

    def assert_deletable(self, name: str, force: bool = False) -> Team:
        """删除前置校验（单独抽出来，方便调用方在「拒绝」路径上什么都不动）。"""
        team = self.get(name)
        if team is None:
            raise TeamNotFoundError(f"团队不存在：{name}")
        # Lead 是调用方本身（is_active 恒为 None），不算「需要等它收工」的成员
        busy = [
            m.name
            for m in team.members
            if m.name != "lead" and m.is_active is not False
        ]
        if busy and not force:
            raise TeamHasActiveMembersError(
                f"团队还有活跃成员：{', '.join(busy)}（强制删除加 force=True）"
            )
        return team

    def delete(self, name: str, force: bool = False) -> dict:
        """删团队；非强制时有成员没空闲就拒绝。"""
        team = self.assert_deletable(name, force)

        warnings: list[str] = []
        for member in team.members:
            if member.name == "lead":
                continue
            if member.worktree_path:
                try:
                    self.wt_mgr.remove(
                        f"{team.sanitized_name}/{member.name}", discard_changes=True
                    )
                except Exception as e:  # 删不掉只告警，不能把删团队卡死
                    warnings.append(f"{member.name}: 删 worktree 失败（{e}）")
            if member.session_dir:
                shutil.rmtree(member.session_dir, ignore_errors=True)
        shutil.rmtree(team.config_dir, ignore_errors=True)
        self.teams.pop(team.sanitized_name, None)
        return {
            "team": team.sanitized_name,
            "removed_members": [m.name for m in team.members if m.name != "lead"],
            "warnings": warnings,
        }

    # ------------------------------------------------------------ 内部

    def _unique(self, base: str) -> str:
        if base not in self.teams:
            return base
        index = 2
        while f"{base}-{index}" in self.teams:
            index += 1
        return f"{base}-{index}"

    def _scan(self) -> None:
        try:
            children = sorted(self.teams_root.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir():
                continue
            config = child / "config.json"
            if not config.is_file():
                continue
            try:
                team = Team.load(config)
            except Exception as e:  # 单个坏配置只跳过（F4）
                logger.warning("团队配置损坏，已跳过：%s（%s）", config, e)
                continue
            self.teams[team.sanitized_name] = team
