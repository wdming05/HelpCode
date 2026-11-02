"""内置样板 Skill：commit / review / test。以 Markdown 常量分发，避免包数据依赖。"""

from __future__ import annotations

from .frontmatter import parse_skill
from .types import Skill

_COMMIT_MD = """---
name: commit
description: 按规范生成 git 提交信息
tools: [read_file, grep, bash]
mode: shared
---
你是提交信息生成助手。当用户要求提交代码时，按以下步骤产出提交信息：

1. 先用 `grep` 或 `bash git diff` 查看当前未提交的改动，理解这次改了什么。
2. 用一句话概括改动意图，作为标题（不超过 50 字符，中文）。
3. 若改动跨多个主题，在正文里用 `-` 分行列出要点。
4. 输出最终提交信息，供用户 `git commit -m` 使用；不要替用户直接提交。

如果用户传入参数：{{args}}
"""

_REVIEW_MD = """---
name: review
description: 审查指定范围（默认当前未提交改动）
tools: [read_file, grep, glob]
mode: shared
---
你是资深代码评审员。按以下 SOP 审查代码：

1. 定位改动：用 `grep` / `git diff`（通过 bash）找出改动文件与行号。
2. 按「正确性 / 可维护性 / 安全风险」三档逐条给结论，每条落到具体文件与行号。
3. 只指出真实问题，不要泛泛而谈；没有问题的部分一笔带过。

审查范围：{{args}}
"""

_TEST_MD = """---
name: test
description: 运行测试并定位失败
tools: [bash, read_file, grep]
mode: shared
---
你是测试执行助手。按以下 SOP 运行并定位失败：

1. 先确认测试命令：查看项目里的测试入口（pytest / npm test / go test）。
2. 运行测试，收集失败用例与报错行。
3. 对每个失败用例，用 read_file / grep 定位根因，给出修复建议。
4. 总结：通过 / 失败各多少，失败根因是什么。

如果用户指定了范围：{{args}}
"""


def _builtin(md: str) -> Skill | None:
    return parse_skill(md, source="builtin")


# 三个样板，解析期即构造；任何一个解析失败都是代码 bug，直接暴露。
BUILTIN_SKILLS: tuple[Skill, ...] = tuple(
    s
    for s in (_builtin(_COMMIT_MD), _builtin(_REVIEW_MD), _builtin(_TEST_MD))
    if s is not None
)
