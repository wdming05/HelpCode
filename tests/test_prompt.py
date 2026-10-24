"""系统提示装配与环境采集的单测。"""

import asyncio

from helpcode.prompt import (
    Environment,
    Module,
    assemble_system,
    build_system_prompt,
    fixed_modules,
    gather_environment,
    optional_modules,
    plan_reminder,
)

# 无参 build_system_prompt() 的 sha256。
# 这个值是「两个可选槽都为空」时算出来的：空 content 会被 assemble_system 跳过，
# 走的代码路径与 ch05 完全相同，所以它与改造前的输出一致。
# 改动任何一个固定模块的文案，这条都会红——这是刻意的：stable 段一旦变形，
# ch05 的 prompt 缓存就整段失效。
_BASELINE_SHA256 = "7d3c2f1e0e9e8ae2b38695b656c4c85994f1732f0146a8b3e53bf3dd06db1b13"


def test_fixed_module_order():
    """七固定模块按 priority 10..70 排列。"""
    names = [m.name for m in fixed_modules()]
    assert names == [
        "身份",
        "系统约束",
        "任务模式",
        "动作执行",
        "工具使用",
        "语气风格",
        "文本输出",
    ]
    priorities = [m.priority for m in fixed_modules()]
    assert priorities == [10, 20, 30, 40, 50, 60, 70]


def test_assemble_by_priority():
    """assemble_system 按 priority 升序拼装，与传入顺序无关。"""
    m85 = Module(name="a", priority=85, content="EIGHTY_FIVE")
    m95 = Module(name="b", priority=95, content="NINETY_FIVE")
    s = assemble_system([m95, m85])  # 乱序传入
    assert s.index("EIGHTY_FIVE") < s.index("NINETY_FIVE")


def test_skip_empty_slots():
    """空 content 的模块被跳过，不留连续空行。"""
    mods = [
        Module(name="有", priority=10, content="A"),
        Module(name="空", priority=20, content=""),
        Module(name="有2", priority=30, content="B"),
    ]
    s = assemble_system(mods)
    assert s == "A\n\nB"
    assert "\n\n\n" not in s


def test_build_deterministic():
    """连续两次 build_system_prompt 逐字节一致。"""
    assert build_system_prompt() == build_system_prompt()


def test_stable_has_no_env():
    """稳定文本不含环境/时间/版本内容。"""
    s = build_system_prompt()
    assert "工作目录" not in s
    assert "平台" not in s
    assert "日期" not in s
    assert "Git 状态" not in s


def test_double_reinforcement():
    """双重强化文案存在于工具使用模块。"""
    s = build_system_prompt()
    assert "优先用专用工具（read_file/glob/grep）而非 bash 拼凑" in s
    assert "编辑文件前必须先 read_file 读取" in s


def test_mount_extend():
    """新增模块自动按 priority 插入到末尾（空槽之后）。"""
    extra = Module(name="测试", priority=85, content="EXTRA")
    s = assemble_system(fixed_modules() + optional_modules() + [extra])
    assert s.endswith("EXTRA")


def test_plan_reminder_tag():
    """plan_reminder 带 system-reminder 标签，完整/精简不同。"""
    full = plan_reminder(True)
    brief = plan_reminder(False)
    assert full != brief
    assert full.startswith("<system-reminder>")
    assert full.endswith("</system-reminder>")


def test_environment_render():
    """Environment.render 含各项，空值项省略。"""
    env = Environment(
        working_dir="/x",
        platform="win32",
        date="2026-10-22",
        git_status="",
        version="dev",
        model="m",
    )
    r = env.render()
    assert "工作目录: /x" in r
    assert "平台: win32" in r
    assert "版本: dev" in r
    assert "Git 状态" not in r  # 空值省略


def test_gather_environment_fields():
    """gather_environment 六字段齐全。"""
    env = asyncio.run(gather_environment("dev", "m"))
    assert env.platform  # 平台非空
    assert env.date  # 日期非空
    assert env.version == "dev"
    assert env.model == "m"


# ---------------------------------------------------------------- 两个可选槽（ch09）


def test_default_unchanged():
    """无参调用与改造前**逐字节相同**（AC8）。

    用 sha256 而不是「包含某句」：少一个换行、多一个空段也必须被这条拦住——
    ch05 的 prompt 缓存按前缀逐字节命中，差一个字节就是整段失效。
    """
    import hashlib

    text = build_system_prompt()
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == _BASELINE_SHA256
    assert "\n\n\n" not in text  # 空槽没有留下多余空行


def test_instructions_filled():
    """指令非空时出现在「工具使用」之后。(AC8)"""
    text = build_system_prompt(instructions="项目规则：提交前跑 ruff")

    assert "项目规则：提交前跑 ruff" in text
    # 「优先用专用工具」是「工具使用」模块的正文，用它做位置锚点
    assert text.index("优先用专用工具") < text.index("项目规则：提交前跑 ruff")


def test_memory_filled_after_instructions():
    """记忆在指令之后（priority 100 > 80）。(AC8)"""
    text = build_system_prompt(instructions="指令段", memory="记忆段")

    assert text.index("指令段") < text.index("记忆段")


def test_only_memory_no_blank_section():
    """只填记忆时不该留下空段。"""
    text = build_system_prompt(memory="只有记忆")
    assert text.count("只有记忆") == 1
    assert "\n\n\n" not in text


def test_empty_strings_same_as_no_args():
    """显式传两个空串，结果与不传完全一致。"""
    assert build_system_prompt("", "") == build_system_prompt()
