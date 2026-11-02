"""生成 3 道本地合成题，用于在没有 Docker 的机器上验证整条评测链路。

为什么不用官方 SWE-bench-Live 的题做这件事：官方题依赖几十 GB 的镜像，
而我们要验证的是「跑题 → 收 patch → 打测试 → 判定 → 归因」这条链。
合成题把这条链原样跑一遍，且完全可复现。

协议对齐：**测试文件不在 base commit 里**，只在评测时通过 test_patch 注入。
agent 拿到的只有 issue 描述——和真实 benchmark 一致，不能提前看测试。

用法：python tests/fixtures/build_mini_dataset.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "eval_datasets" / "mini"
REPO = TARGET / "repos" / "shopcalc"

CALCULATOR = '''"""购物车金额计算。"""


class Calculator:
    """按单价与数量算金额。"""

    def subtotal(self, price: float, quantity: int) -> float:
        return price

    def total(self, price: float, quantity: int) -> float:
        return self.subtotal(price, quantity)
'''

DISCOUNT = '''"""满减折扣。"""


def discount_rate(amount: float) -> float:
    """满 100 打 9 折，满 500 打 8 折。"""
    if amount > 500:
        return 0.8
    if amount > 100:
        return 0.9
    return 1.0


def apply_discount(amount: float) -> float:
    """按折扣率算应付金额，保留两位小数。"""
    return round(amount * discount_rate(amount), 2)
'''

README = """# shopcalc

购物车金额计算库。

- `calculator.Calculator.subtotal(price, quantity)`：算小计
- `discount.discount_rate(amount)`：查折扣率
- `discount.apply_discount(amount)`：算折后金额

运行测试：`python -m pytest`
"""

TEST_SUBTOTAL = """from calculator import Calculator


def test_subtotal_single():
    assert Calculator().subtotal(10, 1) == 10


def test_subtotal_quantity():
    assert Calculator().subtotal(10, 3) == 30


def test_total_quantity():
    assert Calculator().total(25.5, 4) == 102.0
"""

TEST_TAX = """from calculator import apply_tax


def test_tax_basic():
    assert apply_tax(100, 0.13) == 113.0


def test_tax_rounding():
    assert apply_tax(19.99, 0.06) == 21.19


def test_tax_zero_rate():
    assert apply_tax(50, 0) == 50.0
"""

TEST_DISCOUNT = """from discount import apply_discount, discount_rate


def test_discount_below_threshold():
    assert discount_rate(99.99) == 1.0


def test_discount_boundary():
    assert discount_rate(100) == 0.9


def test_discount_500():
    assert discount_rate(500) == 0.8


def test_apply_discount_rounding():
    assert apply_discount(100) == 90.0
"""

ISSUE_SUBTOTAL = """subtotal 在数量大于 1 时算错。

复现：
>>> from calculator import Calculator
>>> Calculator().subtotal(10, 3)
10      # 期望 30

看起来只返回了单价，没有乘数量。total 也跟着错，因为它是调 subtotal 算的。
只要修 subtotal 就够了，total 不用动。
"""

ISSUE_TAX = """需要新增一个含税金额的函数。

要求：在 calculator.py 里增加模块级函数 apply_tax(amount, rate)，
返回「本金 + 税额」的总额，结果四舍五入到两位小数。

例：
apply_tax(100, 0.13) == 113.0
apply_tax(19.99, 0.06) == 21.19
apply_tax(50, 0) == 50.0
"""

ISSUE_DISCOUNT = """折扣的边界判断错了一位。

文案说「满 100 打 9 折，满 500 打 8 折」，也就是刚好 100 元应该打 9 折、
刚好 500 元应该打 8 折。现在 100 和 500 都不打折。

复现：
>>> from discount import discount_rate, apply_discount
>>> discount_rate(100)
1.0     # 期望 0.9
>>> apply_discount(100)
100.0   # 期望 90.0
>>> discount_rate(500)
0.9     # 期望 0.8

99.99 这种不满 100 的必须仍然不打折，别把边界改宽了。
"""


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd or REPO),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout


def build() -> None:
    if TARGET.exists():
        shutil.rmtree(TARGET)
    (REPO / "tests").mkdir(parents=True)
    (REPO / "calculator.py").write_text(CALCULATOR, encoding="utf-8")
    (REPO / "discount.py").write_text(DISCOUNT, encoding="utf-8")
    (REPO / "README.md").write_text(README, encoding="utf-8")

    _git("init", "--quiet")
    _git("config", "user.email", "eval@example.com")
    _git("config", "user.name", "eval")
    _git("add", "-A")
    _git("commit", "--quiet", "-m", "base: 带 bug 的初始版本")
    base = _git("rev-parse", "HEAD").strip()

    patches_dir = TARGET / "test_patches"
    patches_dir.mkdir(parents=True)

    specs = [
        {
            "instance_id": "mini__subtotal-1",
            "files": {"tests/test_subtotal.py": TEST_SUBTOTAL},
            "issue": ISSUE_SUBTOTAL,
            "f2p": [
                "tests/test_subtotal.py::test_subtotal_quantity",
                "tests/test_subtotal.py::test_total_quantity",
            ],
            "p2p": ["tests/test_subtotal.py::test_subtotal_single"],
        },
        {
            "instance_id": "mini__tax-2",
            "files": {
                "tests/test_tax.py": TEST_TAX,
                "tests/test_subtotal.py": TEST_SUBTOTAL,
            },
            "issue": ISSUE_TAX,
            "f2p": [
                "tests/test_tax.py::test_tax_basic",
                "tests/test_tax.py::test_tax_rounding",
                "tests/test_tax.py::test_tax_zero_rate",
            ],
            "p2p": ["tests/test_subtotal.py::test_subtotal_single"],
        },
        {
            "instance_id": "mini__discount-3",
            "files": {"tests/test_discount.py": TEST_DISCOUNT},
            "issue": ISSUE_DISCOUNT,
            "f2p": [
                "tests/test_discount.py::test_discount_boundary",
                "tests/test_discount.py::test_discount_500",
                "tests/test_discount.py::test_apply_discount_rounding",
            ],
            "p2p": ["tests/test_discount.py::test_discount_below_threshold"],
        },
    ]

    rows: list[dict] = []
    for spec in specs:
        for rel, content in spec["files"].items():
            path = REPO / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        _git("add", "-A")
        patch = _git("diff", "--cached", "--text", "--no-color")
        (patches_dir / f"{spec['instance_id']}.patch").write_text(
            patch, encoding="utf-8"
        )
        _git("reset", "--hard", "--quiet")
        _git("clean", "-fdq")

        rows.append(
            {
                "instance_id": spec["instance_id"],
                "repo": "local/shopcalc",
                "base_commit": base,
                "problem_statement": spec["issue"],
                "patch": "",
                "test_patch": patch,
                "FAIL_TO_PASS": spec["f2p"],
                "PASS_TO_PASS": spec["p2p"],
                "test_cmds": ["{python} -m pytest"],
                "local_repo": "repos/shopcalc",
            }
        )

    (TARGET / "dataset.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    (TARGET / "README.md").write_text(
        "# 本地迷你题集\n\n"
        f"由 `tests/fixtures/build_mini_dataset.py` 生成，共 {len(rows)} 题。\n"
        f"base commit：`{base}`\n\n"
        "每题只给 issue 描述；测试文件在评测时通过 `test_patch` 注入，\n"
        "与 SWE-bench 协议一致（agent 不能提前看测试）。\n",
        encoding="utf-8",
    )
    print(f"已生成 {len(rows)} 题 → {TARGET}")
    print(f"base commit {base[:10]}")


if __name__ == "__main__":
    sys.exit(build())
