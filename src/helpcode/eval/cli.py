"""``helpcode eval`` 子命令：跑一批题、出报告。

命令刻意与官方脚本保持一致，便于对照：
``run_evaluation`` 负责「判定」，这里负责「跑 agent + 收 patch + 判定 + 汇总」。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from ..config import Config, ConfigError, load
from .dataset import DatasetError, load_instances
from .driver import make_driver
from .report import write_report
from .runner import EvalRunner

CONFIG_PATH = ".helpcode/config.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helpcode eval", description="在 SWE-bench 风格题集上评测本 agent"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="跑一批题并出报告")
    run.add_argument(
        "--dataset",
        required=True,
        help="本地 JSONL/JSON 路径，或 hf:SWE-bench-Live/SWE-bench-Live",
    )
    run.add_argument(
        "--split", default="lite", help="HF split（lite/verified/test/full）"
    )
    run.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    run.add_argument("--ids", default="", help="逗号分隔的 instance_id")
    run.add_argument("--driver", default="local", choices=["local", "docker"])
    run.add_argument("--provider", default="", help="用哪个 provider（默认第一个）")
    run.add_argument("--model", default="", help="覆盖 provider 的 model")
    run.add_argument("--max-turns", type=int, default=30)
    run.add_argument("--timeout", type=float, default=900.0, help="单题墙钟上限（秒）")
    run.add_argument(
        "--retries", type=int, default=1, help="空补丁时追加提醒重试的次数（默认 1）"
    )
    run.add_argument(
        "--trials",
        type=int,
        default=1,
        help="每题采样次数（>1 时报告 pass@1 / pass^3）",
    )
    run.add_argument("--out", default="runs", help="输出根目录")
    run.add_argument("--run-id", default="", help="运行名（默认时间戳）")

    rep = sub.add_parser("report", help="对已有 run 目录重算报告")
    rep.add_argument("run_dir")
    return parser


def eval_main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "report":
        return _report(Path(args.run_dir))
    return _run(args)


def _report(run_dir: Path) -> int:
    try:
        summary = write_report(run_dir, title=f"评测报告 · {run_dir.name}")
    except FileNotFoundError as e:
        print(f"报告失败：{e}", file=sys.stderr)
        return 1
    print(
        f"解决 {summary['resolved']}/{summary['total']}（{summary['resolved_rate']:.1%}）"
    )
    print(f"报告：{run_dir / 'report.md'}")
    return 0


def _run(args) -> int:
    try:
        cfg = load(CONFIG_PATH)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 1

    provider_cfg = _pick_provider(cfg, args.provider, args.model)
    if provider_cfg is None:
        print(
            "没有可用的 provider，先在 .helpcode/config.yaml 里配一个", file=sys.stderr
        )
        return 1

    ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    try:
        instances = load_instances(
            args.dataset, split=args.split, limit=args.limit or None, instance_ids=ids
        )
    except DatasetError as e:
        print(f"题集加载失败：{e}", file=sys.stderr)
        return 1

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / run_id
    driver = make_driver(args.driver, timeout=args.timeout)

    print(f"题数 {len(instances)} · 后端 {driver.name} · 模型 {provider_cfg.model}")
    print(f"输出 {out_dir}")
    runner = EvalRunner(
        provider_cfg,
        driver,
        out_dir,
        max_turns=args.max_turns,
        timeout=args.timeout,
        retries=args.retries,
        trials=args.trials,
    )
    asyncio.run(runner.run_all(instances))
    summary = write_report(out_dir, title=f"评测报告 · {run_id}")

    print()
    print(
        f"解决率 {summary['resolved']}/{summary['total']} = {summary['resolved_rate']:.1%}"
    )
    if args.trials > 1:
        print(f"pass@1（trial 成功率） {summary['pass_at_1']:.1%}")
        print(f"pass^3（3 次全过） {summary['pass_pow_3']:.1%}")
    if summary["env_failed"]:
        print(
            f"有效分母 {summary['valid']}（排除环境失败 {summary['env_failed']}）"
            f" → {summary['resolved_rate_valid']:.1%}"
        )
    print(f"报告：{out_dir / 'report.md'}")
    return 0


def _pick_provider(cfg: Config, name: str, model: str):
    """选 provider；``--model`` 只覆盖模型名，不改协议与端点。"""
    from dataclasses import replace

    chosen = None
    if name:
        chosen = next((p for p in cfg.providers if p.name == name), None)
    if chosen is None and cfg.providers:
        chosen = cfg.providers[0]
    if chosen is not None and model:
        chosen = replace(chosen, model=model)
    return chosen
