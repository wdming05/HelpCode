"""pytest 全局配置：fixtures 导入路径 + 「压缩开启」回归开关。"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))


@pytest.fixture(autouse=True)
def _force_compress(monkeypatch):
    """``FORCE_COMPRESS=1`` 时把自动压缩阈值压到极小。

    这是 T16 要求的「压缩开启状态下的全量回归」：默认测试里压缩几乎不会触发，
    只有强行拉低阈值，才能暴露"某处假设了历史只增不减"。
    """
    if os.environ.get("FORCE_COMPRESS") == "1":
        from helpcode.context import budget

        monkeypatch.setattr(budget, "SAFETY_AUTO", 100)
