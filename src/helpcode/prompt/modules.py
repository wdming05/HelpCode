"""系统提示的固定/可选模块定义与装配。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    """一个系统提示模块：名称、优先级（升序拼装）、正文（空串表示空槽）。"""

    name: str
    priority: int
    content: str


def fixed_modules() -> list[Module]:
    """七个固定模块，priority 依次 10..70。"""
    return [
        Module(
            name="身份",
            priority=10,
            content="你是一个终端 AI 编程助手（HelpCode），能通过调用工具来完成实际工作。",
        ),
        Module(
            name="系统约束",
            priority=20,
            content=(
                "始终遵守安全边界：不执行可能造成破坏的危险命令；"
                "不泄露 API 密钥或任何敏感信息；不越权访问超出任务范围的数据。"
            ),
        ),
        Module(
            name="任务模式",
            priority=30,
            content=(
                "你可以连续多步调用工具，自主完成一个完整任务："
                "先调用工具获取所需信息，再基于工具返回的结果继续推进，直到任务完成。"
            ),
        ),
        Module(
            name="动作执行",
            priority=40,
            content="执行操作时遵循：先观察再动手；修改文件或执行命令前，先用只读工具了解现状。",
        ),
        Module(
            name="工具使用",
            priority=50,
            content=(
                "你可以使用这些工具：read_file（读文件）、write_file（写文件）、"
                "edit_file（改文件）、bash（执行命令）、glob（按模式找文件）、grep（搜代码内容）。"
                "优先用专用工具（read_file/glob/grep）而非 bash 拼凑；编辑文件前必须先 read_file 读取。"
            ),
        ),
        Module(
            name="语气风格",
            priority=60,
            content="请用简洁、准确的中文回答。",
        ),
        Module(
            name="文本输出",
            priority=70,
            content="涉及代码时给出清晰、可执行的答案，代码放在代码块中，并附必要解释。",
        ),
    ]


def optional_modules(instructions: str = "", memory: str = "") -> list[Module]:
    """三个可选槽，默认全空、装配时跳过。

    - ``instructions``（80）：项目指令文件的内容，规则类，排在记忆之前
    - 已激活 Skill（90）：仍留空，ch07 之后的事
    - ``memory``（100）：记忆索引，参考类，排在最后（最靠近对话）

    都为空时装配结果与 ch05 **逐字节相同**（AC8）——默认参数保证了这一点。
    """
    return [
        Module(name="自定义指令", priority=80, content=instructions),
        Module(name="已激活 Skill", priority=90, content=""),
        Module(name="长期记忆", priority=100, content=memory),
    ]


def assemble_system(mods: list[Module]) -> str:
    """按 priority 升序、跳过空 content、用空行连接。"""
    ordered = sorted(mods, key=lambda m: m.priority)
    parts = [m.content for m in ordered if m.content]
    return "\n\n".join(parts)
