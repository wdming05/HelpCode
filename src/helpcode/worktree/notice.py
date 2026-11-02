"""注入给子 Agent 的 Worktree 说明文本。

为什么必须注入：子 Agent 看到的系统提示是角色正文，它不知道自己的工作目录
已经被换成 Worktree。不说清楚，它可能写绝对路径、或者提示主 Agent「去改
<父目录>/x」，隔离就白做了。
"""

from __future__ import annotations

_PLACEHOLDER = """## 工作目录

你当前在一个**独立的 Git Worktree**里工作：

- 工作目录：{wt}
- 主仓库目录：{parent}（**不要动它**）

规则：
1. 所有文件操作用相对路径即可，相对路径都解析到上面这个工作目录。
2. 不要 `cd` 到主仓库目录去改文件——那是主 Agent 的工作区，改那里等于绕过了隔离。
3. 改完就结束，不需要合并、不需要切分支，主 Agent 会自己 review 你的改动。
"""


def build_worktree_notice(parent_cwd: str, wt_path: str) -> str:
    """生成路径说明，拼在任务文本前。"""
    return _PLACEHOLDER.format(wt=wt_path, parent=parent_cwd)
