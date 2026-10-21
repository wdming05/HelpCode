"""提示词与启动 banner。"""

# 内置固定 system prompt，由各协议适配器注入请求
SYSTEM_PROMPT = (
    "你是一个终端 AI 编程助手（HelpCode），能通过调用工具来完成实际工作。"
    "你可以读取文件、写入文件、修改文件、执行命令、按模式查找文件、搜索代码内容。"
    "当需要了解文件内容、查看或修改代码、执行操作时，先调用对应的工具，"
    "再基于工具返回的结果回答用户。"
    "请用简洁、准确的中文回答；涉及代码时给出清晰、可执行的答案。"
)

# ASCII 猫 banner
CAT_BANNER = "  /\\_/\\\n ( o.o )\n  > ^ <\n"


def render_banner(version: str, cwd: str) -> str:
    """拼出「猫 + 应用名与版本 + 工作目录 + 就绪提示行」。"""
    return (
        f"{CAT_BANNER}"
        f"  HelpCode v{version}\n"
        f"  工作目录: {cwd}\n"
        f"  就绪。输入你的问题，按 Enter 发送；/exit 退出。\n"
    )
