"""Skill 注册中心：按名持有 Skill，支持同名覆盖与列举。"""

from __future__ import annotations

from .types import Skill


class SkillRegistry:
    """已加载的 Skill 集合。同名覆盖（高优先级后写入）。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._order: list[str] = []
        self.warnings: list[str] = []

    def set(self, skill: Skill) -> None:
        """写入一个 Skill；同名覆盖，首次出现时记录顺序。"""
        if skill.name not in self._skills:
            self._order.append(skill.name)
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name.strip().lower())

    def all(self) -> list[Skill]:
        return [self._skills[n] for n in self._order]

    def __len__(self) -> int:
        return len(self._order)
