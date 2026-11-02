"""判定：一道题算不算「修好」。

只有一条规则，和官方一致：

    resolved = FAIL_TO_PASS 全过 AND PASS_TO_PASS 全过

前者证明修好了，后者证明没修坏别的。归因（reason）用于区分失败类型：
同样是没 resolved，「空补丁」「只改测试」「改坏了别的」是完全不同的失败。
"""

from __future__ import annotations

from dataclasses import dataclass

from .driver import TestOutcome
from .instance import Instance
from .patch import is_empty, is_test_only

RESOLVED = "resolved"
EMPTY_PATCH = "empty_patch"
TEST_ONLY = "test_only"
F2P_FAILED = "f2p_failed"
P2P_BROKEN = "p2p_broken"
JUDGE_ERROR = "judge_error"
NO_TESTS = "no_tests"


@dataclass
class Verdict:
    """判定结果 + 计数，够写报告也够做归因。"""

    resolved: bool
    reason: str
    detail: str = ""
    f2p_total: int = 0
    f2p_passed: int = 0
    p2p_total: int = 0
    p2p_passed: int = 0

    def to_dict(self) -> dict:
        return {
            "resolved": self.resolved,
            "reason": self.reason,
            "detail": self.detail,
            "f2p": f"{self.f2p_passed}/{self.f2p_total}",
            "p2p": f"{self.p2p_passed}/{self.p2p_total}",
        }


def _score(
    inst: Instance, outcome: TestOutcome
) -> tuple[int, int, int, int, list[str]]:
    """统计 F2P / P2P 的通过数，并收集没过的 F2P 名字。"""
    f2p_pass = sum(1 for x in inst.fail_to_pass if outcome.passed(x))
    p2p_pass = sum(1 for x in inst.pass_to_pass if outcome.passed(x))
    missing = [x for x in inst.fail_to_pass if not outcome.passed(x)]
    return f2p_pass, len(inst.fail_to_pass), p2p_pass, len(inst.pass_to_pass), missing


def judge(inst: Instance, outcome: TestOutcome, patch: str) -> Verdict:
    """按顺序判定：执行错误 → 空补丁 → 只改测试 → F2P → P2P → resolved。"""
    if outcome.error:
        return Verdict(False, JUDGE_ERROR, outcome.error)

    if is_empty(patch):
        return Verdict(False, EMPTY_PATCH, "模型没有产出任何改动")

    if is_test_only(patch):
        return Verdict(False, TEST_ONLY, "改动只落在测试文件上——即使测试变绿也不算修好")

    f2p_pass, f2p_total, p2p_pass, p2p_total, missing = _score(inst, outcome)
    if f2p_total == 0 and p2p_total == 0:
        return Verdict(False, NO_TESTS, "题集没有给 FAIL_TO_PASS / PASS_TO_PASS")

    if f2p_pass < f2p_total:
        shown = "、".join(x.split("::")[-1] for x in missing[:3])
        more = f" 等 {len(missing)} 个" if len(missing) > 3 else ""
        return Verdict(
            resolved=False,
            reason=F2P_FAILED,
            detail=f"目标测试仍有未通过：{shown}{more}",
            f2p_total=f2p_total,
            f2p_passed=f2p_pass,
            p2p_total=p2p_total,
            p2p_passed=p2p_pass,
        )

    if p2p_pass < p2p_total:
        return Verdict(
            resolved=False,
            reason=P2P_BROKEN,
            detail=f"回归：{p2p_total - p2p_pass} 个原本通过的测试挂了",
            f2p_total=f2p_total,
            f2p_passed=f2p_pass,
            p2p_total=p2p_total,
            p2p_passed=p2p_pass,
        )

    return Verdict(
        resolved=True,
        reason=RESOLVED,
        detail="目标测试全过且无回归",
        f2p_total=f2p_total,
        f2p_passed=f2p_pass,
        p2p_total=p2p_total,
        p2p_passed=p2p_pass,
    )
