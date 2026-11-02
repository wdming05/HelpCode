"""build_system_prompt 的 active_skills / available_skills 槽位与默认不变。"""

from helpcode.prompt import build_system_prompt


def test_build_system_prompt_skills():
    text = build_system_prompt(
        active_skills="# Skill: x\nSOP", available_skills="- x：d"
    )
    assert "# Skill: x" in text
    assert "SOP" in text
    assert "- x：d" in text


def test_active_before_memory():
    text = build_system_prompt(memory="记忆段", active_skills="技能段")
    assert text.index("技能段") < text.index("记忆段")


def test_default_unchanged():
    # 无任何 Skill 时与本章前逐字节一致（AC17）
    assert build_system_prompt() == build_system_prompt(
        active_skills="", available_skills=""
    )
