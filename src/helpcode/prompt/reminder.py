"""补充消息与规划提醒。"""


def system_reminder(body: str) -> str:
    """用特殊标签包裹一段系统补充指令。"""
    return f"<system-reminder>\n{body}\n</system-reminder>"


_PLAN_REMINDER_FULL = (
    "你当前处于计划模式（Plan Mode）：只能使用只读工具（read_file、glob、grep）"
    "了解现状，然后输出一份清晰的分步执行计划。"
    "不要修改文件、不要执行命令。等待用户用 /do 批准后再执行。"
)

_PLAN_REMINDER_CONCISE = "计划模式：仅只读工具，输出计划后等待 /do。"


def plan_reminder(full: bool) -> str:
    """返回带标签的规划模式提醒：full=True 完整版，否则精简版。"""
    return system_reminder(_PLAN_REMINDER_FULL if full else _PLAN_REMINDER_CONCISE)


# 执行指令（/do 时注入，触发模型按计划执行）
EXECUTE_DIRECTIVE = "切换到执行模式，请按照上面讨论的计划开始执行。"
