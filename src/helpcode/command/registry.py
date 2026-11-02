"""命令注册中心：名称与别名共享命名空间，注册即查重。"""

from __future__ import annotations

from .types import Command


class DuplicateCommandError(RuntimeError):
    """同一条命令键被注册两次。启动期必须终止进程。"""

    def __init__(self, key: str, first: str, second: str) -> None:
        self.key = key
        self.first = first
        self.second = second
        super().__init__(f"命令名冲突：/{key} 同时被 /{first} 与 /{second} 占用")


class CommandRegistry:
    """持有命令并负责查找、列举与补全候选。"""

    def __init__(self) -> None:
        self._by_key: dict[str, Command] = {}
        self._ordered: list[Command] = []

    def register(self, cmd: Command) -> None:
        for key in cmd.names():
            existing = self._by_key.get(key)
            if existing is not None:
                raise DuplicateCommandError(key, existing.name, cmd.name)
        self._ordered.append(cmd)
        for key in cmd.names():
            self._by_key[key] = cmd

    def resolve(self, name: str) -> Command | None:
        return self._by_key.get(name.lower())

    def visible(self) -> list[Command]:
        return [c for c in self._ordered if not c.hidden]

    def complete(self, prefix: str) -> list[str]:
        """返回以 ``/`` 开头的候选命令名（含别名），隐藏命令不参与。"""
        key = prefix.lstrip("/").lower()
        hits: list[str] = []
        for cmd in self._ordered:
            if cmd.hidden:
                continue
            for name in cmd.names():
                if name.startswith(key):
                    hits.append(f"/{name}")
        # 短名在前，保证排序稳定（`/s` → session 前 status 前 … 按长度再字典序）
        return sorted(set(hits), key=lambda s: (len(s), s))

    def __len__(self) -> int:
        return len(self._ordered)
