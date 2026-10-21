"""入口：加载配置、启动 TUI。"""

import sys

from .config import ConfigError, load
from .tui.app import HelpCodeApp

CONFIG_PATH = ".helpcode/config.yaml"


def main() -> None:
    """装配与启动 HelpCode。"""
    try:
        cfg = load(CONFIG_PATH)
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        HelpCodeApp(cfg.providers).run()
    except KeyboardInterrupt:
        # Ctrl+C 兜底（Textual 通常已处理）
        pass
    except Exception as e:
        print(f"启动失败: {e}", file=sys.stderr)
        sys.exit(1)
