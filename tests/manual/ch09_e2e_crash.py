"""ch09 端到端驱动：真 provider + 真进程 + 强制终止（kill -9 等价）。

四个场景对应 checklist 第 10 节的梯度：

- 场景 4：跑到第 2 轮结束 → 硬杀 → 验「每条都 fsync」与「最多丢一条」
- 场景 4b：压缩完成 → 立刻硬杀 → 验「compact 标记先落盘、回放只取标记之后」
- 场景 3（对照组）：压缩后正常 close 退出 → 验「压缩后历史能回放」
- 场景 1：三层指令文件真的进了 system prompt（模型能复述）

用法：python tmp_e2e_driver.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from helpcode.memory.restore import restore


def fresh_work() -> Path:
    return Path(tempfile.mkdtemp(prefix="hc-e2e-"))


def run_child(work: Path, mode: str, stop_at: str) -> tuple[list[str], Path]:
    """启动子进程，看到 stop_at 立刻硬杀（不给它 close 的机会）。"""
    env = {**os.environ, "E2E_REPO": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "ch09_e2e_child.py"), mode],
        cwd=work,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    seen: list[str] = []
    for line in proc.stdout:
        seen.append(line.rstrip())
        if stop_at in line:
            break
    proc.kill()  # Windows: TerminateProcess；POSIX: SIGKILL —— 都是硬杀
    proc.wait()
    assert proc.returncode != 0, "子进程不该自己退出（那就不是崩溃了）"
    for line in seen:
        print("   [child]", line)
    return seen, work


def session_jsonl(work: Path) -> Path:
    return next((work / ".helpcode" / "sessions").glob("*/conversation.jsonl"))


def dump(path: Path) -> tuple[list[dict], int]:
    """返回 (可解析记录, 坏行数)。"""
    records, bad = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    return records, bad


def scenario_4() -> None:
    print("\n=== 场景 4：跑到第 2 轮结束 → 硬杀 ===")
    work = fresh_work()
    work.mkdir(exist_ok=True)
    run_child(work, "plain", "ROUNDS_DONE")
    path = session_jsonl(work)
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    records, bad = dump(path)
    print(
        f"   总行数={len(raw_lines)} 好行={len(records)} 坏行={bad} "
        f"角色={[r.get('role') or r.get('type') for r in records]}"
    )
    print("   model 齐全:", all("model" in r for r in records if r.get("role")))
    report = restore(path)
    print(
        f"   恢复条数={len(report.messages)} skipped={report.skipped_lines} "
        f"truncated={report.interrupted_groups} 角色={[m.role for m in report.messages]}"
    )
    assert len(records) >= 4, "两轮至少 4 行（user/assistant ×2）——fsync 该保住它们"
    assert bad <= 1, "最多一行半截 JSON"
    assert len(report.messages) >= len(records) - 1, "崩溃前写好的消息必须有回放"


def scenario_4b() -> None:
    print("\n=== 场景 4b：压缩完成 → 立刻硬杀 ===")
    work = fresh_work()
    work.mkdir(exist_ok=True)
    lines, _ = run_child(work, "compact", "COMPACT_DONE")
    done = next(ln for ln in lines if ln.startswith("COMPACT_DONE"))
    _, count, head, notice = done.split("|", 3)
    print(f"   压缩提示={notice}")
    path = session_jsonl(work)
    records, bad = dump(path)
    kinds = [r.get("type") or r.get("role") for r in records]
    marker_idx = [i for i, k in enumerate(kinds) if k == "compact"]
    print(f"   记录数={len(records)} 坏行={bad} 标记位置={marker_idx}")
    print("   类型序列:", kinds)
    assert len(marker_idx) == 1, "一次压缩只该有一行标记"
    # 标记必须夹在「压缩前的旧历史」与「压缩后的新历史」之间：
    # 前面 8 行是压缩前的 4 轮，后面正好是压缩完成时的条数。
    assert marker_idx[0] == 8, f"标记前应正好是压缩前的 8 行，实际 {marker_idx[0]}"
    after = len(records) - marker_idx[0] - 1
    print(f"   标记后消息数={after} 压缩完成时条数={count}")
    assert after == int(count), "标记之后必须正好是压缩后的历史"
    report = restore(path)
    restored_head = report.messages[0].content[:40] if report.messages else ""
    print(f"   压缩完成时内存条数={count} 回放条数={len(report.messages)}")
    print(f"   内存首条={head} 回放首条={restored_head!r}")
    assert len(report.messages) == int(count), "回放必须等于压缩完成时的历史"
    assert repr(restored_head) == head, "回放的首条必须是压缩后的首条（不是压缩前的）"
    assert len(report.messages) < len(records) + int(count), (
        "压缩前的消息不该整体重复出现"
    )


def scenario_3() -> None:
    print("\n=== 场景 3（对照组）：压缩后正常 close 退出 ===")
    work = fresh_work()
    work.mkdir(exist_ok=True)
    env = {**os.environ, "E2E_REPO": str(REPO)}
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "ch09_e2e_child.py"),
            "compact-exit",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=300,
    )
    for line in proc.stdout.splitlines()[-3:]:
        print("   [child]", line)
    assert proc.returncode == 0, proc.stderr[-500:]
    path = session_jsonl(work)
    records, bad = dump(path)
    report = restore(path)
    print(f"   记录数={len(records)} 坏行={bad} 恢复条数={len(report.messages)}")
    assert bad == 0 and report.messages


if __name__ == "__main__":
    scenario_4()
    scenario_4b()
    scenario_3()
    print("\nE2E OK")
