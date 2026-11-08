"""汇总报告：把逐题结果变成可汇报的数字。

两个分母都要报：

- **总分母**：所有题。诚实但会被环境问题拉低。
- **有效分母**：排除 ``judge_error`` / ``prepare_failed`` / ``no_tests``——
  这些是环境或题集问题，不是模型能力。

同时报「有效分母 + 排除原因」，比只报一个漂亮数字可信得多。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

# 这些失败原因不属于模型能力问题
ENV_REASONS = frozenset({"judge_error", "prepare_failed", "no_tests", "patch_failed"})

REASON_LABEL = {
    "resolved": "修好",
    "empty_patch": "空补丁（没动手）",
    "test_only": "只改了测试",
    "f2p_failed": "目标测试没过",
    "p2p_broken": "改坏了别的",
    "judge_error": "判定环境出错",
    "prepare_failed": "工作区准备失败",
    "patch_failed": "补丁收集失败",
    "no_tests": "题集没给测试清单",
    "over_modified": "过度修改（无需修改题）",
}


def load_results(run_dir: Path) -> list[dict]:
    path = run_dir / "results.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"找不到 {path}")
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def summarize(results: list[dict]) -> dict:
    """算所有指标；纯函数，方便单测。"""
    total = len(results)
    resolved = [r for r in results if r.get("resolved")]
    env_failed = [r for r in results if r.get("reason") in ENV_REASONS]
    valid = total - len(env_failed)

    reasons = Counter(r.get("reason", "?") for r in results)
    t_in = sum(int(r.get("tokens_in") or 0) for r in results)
    t_out = sum(int(r.get("tokens_out") or 0) for r in results)
    elapsed = sum(float(r.get("elapsed") or 0) for r in results)
    turns = sum(int(r.get("turns") or 0) for r in results)
    tools = sum(int(r.get("tool_calls") or 0) for r in results)
    # 首次就动手的题数：attempts > 1 说明模型空转了一次，靠追问才继续。
    # 这是 harness 鲁棒性的指标，和题目难度无关，单独报出来。
    retried = sum(1 for r in results if int(r.get("attempts") or 1) > 1)

    # 多采样指标：
    # - pass@1：trial 级成功率 = 通过 trial 数 / 总 trial 数
    # - pass^3：3 次采样全部通过的题占比（稳定性）
    trial_lists = [r.get("trial_resolved") or [] for r in results]
    n_trials = sum(len(x) for x in trial_lists)
    n_trial_passed = sum(sum(x) for x in trial_lists)
    all_three = sum(1 for x in trial_lists if len(x) == 3 and all(x))

    return {
        "total": total,
        "resolved": len(resolved),
        "valid": valid,
        "env_failed": len(env_failed),
        "resolved_rate": round(len(resolved) / total, 4) if total else 0.0,
        "resolved_rate_valid": round(len(resolved) / valid, 4) if valid else 0.0,
        "pass_at_1": round(n_trial_passed / n_trials, 4) if n_trials else 0.0,
        "pass_pow_3": round(all_three / total, 4) if total else 0.0,
        "reasons": dict(reasons.most_common()),
        "tokens_in": t_in,
        "tokens_out": t_out,
        "tokens_per_task": round((t_in + t_out) / total) if total else 0,
        "avg_turns": round(turns / total, 1) if total else 0.0,
        "avg_tools": round(tools / total, 1) if total else 0.0,
        "wall_seconds": round(elapsed, 1),
        "avg_seconds": round(elapsed / total, 1) if total else 0.0,
        "retried": retried,
        "retry_rate": round(retried / total, 4) if total else 0.0,
    }


def render_markdown(summary: dict, results: list[dict], *, title: str = "") -> str:
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")

    lines.append("## 总分")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 题数 | {summary['total']} |")
    lines.append(
        f"| 解决的题 | {summary['resolved']}（{summary['resolved_rate']:.1%}） |"
    )
    if any(len(r.get("trial_resolved") or []) > 1 for r in results):
        lines.append(f"| pass@1（trial 成功率） | {summary['pass_at_1']:.1%} |")
        lines.append(f"| pass^3（3 次全过） | {summary['pass_pow_3']:.1%} |")
    lines.append(
        f"| 有效分母 | {summary['valid']}（排除环境失败 {summary['env_failed']}） |"
    )
    lines.append(f"| 有效分母上的解决率 | {summary['resolved_rate_valid']:.1%} |")
    lines.append(f"| 平均轮次 | {summary['avg_turns']} |")
    lines.append(f"| 平均工具调用 | {summary['avg_tools']} |")
    lines.append(f"| 平均 token/题 | {summary['tokens_per_task']:,} |")
    lines.append(f"| 总耗时 | {summary['wall_seconds']:.0f}s |")
    lines.append(
        f"| 空转后追问才动手 | {summary.get('retried', 0)} 题"
        f"（{summary.get('retry_rate', 0):.0%}） |"
    )
    lines.append("")

    lines.append("## 失败归因")
    lines.append("")
    lines.append("| 原因 | 题数 |")
    lines.append("|---|---|")
    for reason, count in summary["reasons"].items():
        lines.append(f"| {REASON_LABEL.get(reason, reason)} | {count} |")
    lines.append("")

    lines.append("## 逐题明细")
    lines.append("")
    lines.append("| 题目 | 结果 | 归因 | F2P | P2P | 轮次 | 工具 | token | 耗时 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        mark = "✅" if r.get("resolved") else "—"
        lines.append(
            f"| {r['instance_id']} | {mark} | {r.get('reason', '')} | {r.get('f2p', '')} "
            f"| {r.get('p2p', '')} | {r.get('turns', 0)} | {r.get('tool_calls', 0)} "
            f"| {int(r.get('tokens_in') or 0) + int(r.get('tokens_out') or 0):,} "
            f"| {float(r.get('elapsed') or 0):.0f}s |"
        )
    lines.append("")
    return "\n".join(lines)


def write_report(run_dir: Path, *, title: str = "") -> dict:
    """读 results.jsonl，写 report.json 与 report.md，返回汇总。"""
    results = load_results(run_dir)
    summary = summarize(results)
    (run_dir / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "report.md").write_text(
        render_markdown(summary, results, title=title), encoding="utf-8"
    )
    return summary
