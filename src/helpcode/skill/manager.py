"""SkillManager：激活集合、阶段渲染、白名单并集、reload。

它只持有 SkillRegistry 与激活状态，不碰 UI，不碰 Agent——独立对话的执行由
上层（app）来做，因为那需要 provider/engine/context 的装配。
"""

from __future__ import annotations

from .frontmatter import substitute
from .registry import SkillRegistry
from .types import LOAD_SKILL_TOOL, Skill


class SkillManager:
    """管理「哪些 Skill 被激活」以及它们的渲染与白名单。"""

    def __init__(self, registry: SkillRegistry, reload_fn=None) -> None:
        self._registry = registry
        self._active: list[str] = []  # 激活顺序（渲染时稳定）
        self._reload_fn = reload_fn

    def get(self, name: str) -> Skill | None:
        return self._registry.get(name)

    def available(self) -> list[Skill]:
        return self._registry.all()

    # ------------------------------------------------------------ 激活

    def activate(self, name: str) -> bool:
        skill = self.get(name)
        if skill is None:
            return False
        if skill.name not in self._active:
            self._active.append(skill.name)
        return True

    def deactivate(self, name: str) -> None:
        key = name.strip().lower()
        if key in self._active:
            self._active.remove(key)

    def clear(self) -> None:
        self._active = []

    def is_active(self, name: str) -> bool:
        return name.strip().lower() in self._active

    def active_names(self) -> tuple[str, ...]:
        return tuple(self._active)

    # ------------------------------------------------------------ 渲染

    def render_available(self) -> str:
        """阶段一：只注入名字 + 一句话说明，不含 SOP 正文。"""
        lines = [f"- {s.name}：{s.description}" for s in self._registry.all()]
        if not lines:
            return ""
        head = "可用 Skill（用 load_skill 工具按需加载完整指令）："
        return head + "\n" + "\n".join(lines)

    def render_active(self) -> str:
        """阶段二：激活 Skill 的完整 SOP，钉在系统提示里，每轮重建。"""
        blocks: list[str] = []
        for name in self._active:
            skill = self._registry.get(name)
            if skill is None:
                continue
            blocks.append(f"# Skill: {skill.name}\n{skill.body}")
        return "\n\n---\n\n".join(blocks)

    # ------------------------------------------------------------ 白名单

    def allowed_tools(self) -> set[str] | None:
        """激活 Skill 的工具白名单并集；无激活返回 ``None``（不限制）。

        ``load_skill`` 恒并入：它是系统级工具，不受白名单收窄（F11）。
        """
        if not self._active:
            return None
        names: set[str] = {LOAD_SKILL_TOOL}
        for name in self._active:
            skill = self._registry.get(name)
            if skill is not None:
                names.update(skill.tools)
        return names

    # ------------------------------------------------------------ 加载正文

    def load_text(self, skill: Skill, args: str) -> str:
        """返回激活后交给模型的完整正文（含参数替换与目录提示）。"""
        body = substitute(skill.body, args)
        header = f"# Skill「{skill.name}」已加载\n\n{body}"
        if skill.directory is not None:
            header += f"\n\n本 Skill 的目录：{skill.directory}（模板 / 示例 / 脚本在此目录下）"
        return header

    # ------------------------------------------------------------ 热更新

    def reload(self) -> list[str]:
        """重新从磁盘加载 Skill 定义；保留仍然存在的激活项。"""
        if self._reload_fn is None:
            return ["当前环境不支持热更新"]
        new_registry = self._reload_fn()
        self._registry = new_registry
        self._active = [n for n in self._active if self._registry.get(n) is not None]
        return new_registry.warnings
