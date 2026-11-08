"""评测子系统的单测：patch 收集、日志解析、判定、报告分母。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from git_repo import git, make_repo

from helpcode.eval import (
    Instance,
    LocalDriver,
    TestOutcome,
    changed_files,
    collect_patch,
    judge,
    load_instances,
    summarize,
)
from helpcode.eval.dataset import DatasetError
from helpcode.eval.instance import from_row
from helpcode.eval.judge import (
    EMPTY_PATCH,
    F2P_FAILED,
    JUDGE_ERROR,
    OVER_MODIFIED,
    P2P_BROKEN,
    RESOLVED,
    TEST_ONLY,
)
from helpcode.eval.patch import is_test_only

# ---------------------------------------------------------------- 数据模型


def test_from_row_accepts_list_and_json_string():
    """HF 里 FAIL_TO_PASS 可能是 list，也可能是 JSON 字符串。"""
    a = from_row({"instance_id": "x-1", "FAIL_TO_PASS": ["t.py::a"]})
    b = from_row({"instance_id": "x-2", "FAIL_TO_PASS": '["t.py::a", "t.py::b"]'})
    assert a.fail_to_pass == ["t.py::a"]
    assert b.fail_to_pass == ["t.py::a", "t.py::b"]


def test_from_row_resolves_local_repo(tmp_path):
    inst = from_row(
        {"instance_id": "x", "local_repo": "repos/r"}, base_dir=str(tmp_path)
    )
    assert Path(inst.local_repo) == (tmp_path / "repos" / "r").resolve()


def test_public_view_hides_answers():
    """给 agent 的只有 problem_statement——test_patch / F2P 不能漏出去。"""
    inst = Instance(
        instance_id="x",
        repo="a/b",
        base_commit="c",
        problem_statement="有个 bug",
        test_patch="diff --git ...",
        fail_to_pass=["t.py::a"],
    )
    view = inst.public_view()
    assert set(view) == {"instance_id", "problem_statement"}


# ---------------------------------------------------------------- patch


@pytest.fixture
def repo(tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "add mod")
    return root


def test_collect_patch_includes_new_file(repo):
    """新建文件必须进 patch。

    官方 README 给的 ``git --no-pager diff HEAD`` 不含未跟踪文件——照着抄
    会得到空补丁，模型做对了也判失败。这里锁住 ``add -A`` 的行为。
    """
    (repo / "new_mod.py").write_text("VALUE = 42\n", encoding="utf-8")
    patch = collect_patch(repo)
    assert "new_mod.py" in patch
    assert "+VALUE = 42" in patch


def test_collect_patch_excludes_caches(repo):
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (repo / ".pytest_cache").mkdir()
    (repo / ".pytest_cache" / "TAG").write_text("x", encoding="utf-8")
    (repo / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    patch = collect_patch(repo)
    files = changed_files(patch)
    assert files == ["mod.py"]


def test_test_only_detection():
    patch = "diff --git a/tests/test_a.py b/tests/test_a.py\n+++ b/tests/test_a.py\n"
    assert is_test_only(patch)
    src = "diff --git a/src/a.py b/src/a.py\n+++ b/src/a.py\n"
    assert not is_test_only(src)


# ---------------------------------------------------------------- 判定


def _inst(**kw) -> Instance:
    base = {
        "instance_id": "x",
        "repo": "a/b",
        "base_commit": "c",
        "problem_statement": "p",
        "fail_to_pass": ["tests/test_a.py::test_f2p"],
        "pass_to_pass": ["tests/test_a.py::test_p2p"],
    }
    base.update(kw)
    return Instance(**base)


def _outcome(results: dict) -> TestOutcome:
    return TestOutcome(results=results)


def test_judge_resolved():
    inst = _inst()
    out = _outcome({("test_a.py", "test_f2p"): True, ("test_a.py", "test_p2p"): True})
    v = judge(inst, out, "diff --git a/src.py b/src.py\n+++ b/src.py\n")
    assert v.resolved and v.reason == RESOLVED


def test_judge_empty_patch():
    v = judge(_inst(), _outcome({}), "")
    assert not v.resolved and v.reason == EMPTY_PATCH


def test_judge_test_only():
    patch = "diff --git a/tests/test_a.py b/tests/test_a.py\n+++ b/tests/test_a.py\n"
    v = judge(_inst(), _outcome({}), patch)
    assert not v.resolved and v.reason == TEST_ONLY


def test_judge_f2p_failed():
    out = _outcome({("test_a.py", "test_f2p"): False, ("test_a.py", "test_p2p"): True})
    v = judge(_inst(), out, "diff --git a/src.py b/src.py\n+++ b/src.py\n")
    assert not v.resolved and v.reason == F2P_FAILED
    assert v.f2p_passed == 0 and v.f2p_total == 1


def test_judge_p2p_broken():
    out = _outcome({("test_a.py", "test_f2p"): True, ("test_a.py", "test_p2p"): False})
    v = judge(_inst(), out, "diff --git a/src.py b/src.py\n+++ b/src.py\n")
    assert not v.resolved and v.reason == P2P_BROKEN
    assert v.p2p_passed == 0 and v.p2p_total == 1


def test_judge_error_wins():
    out = TestOutcome(error="docker 挂了")
    v = judge(_inst(), out, "diff --git a/src.py b/src.py\n+++ b/src.py\n")
    assert v.reason == JUDGE_ERROR


def test_judge_no_change_resolved():
    """无需修改题：patch 为空 + 测试全过 = resolved。"""
    inst = _inst(no_change=True, fail_to_pass=[])
    out = _outcome({("test_a.py", "test_p2p"): True})
    v = judge(inst, out, "")
    assert v.resolved and v.reason == RESOLVED


def test_judge_no_change_over_modified():
    """无需修改题：模型改了文件 → over_modified，即使测试全绿也判失败。"""
    inst = _inst(no_change=True, fail_to_pass=[])
    out = _outcome({("test_a.py", "test_p2p"): True})
    v = judge(inst, out, "diff --git a/src.py b/src.py\n+++ b/src.py\n")
    assert not v.resolved and v.reason == OVER_MODIFIED


# ---------------------------------------------------------------- 驱动


def test_normalize_nodeid_matches_both_forms():
    """数据集写 ``tests/test_a.py::test_x``，junit 给 ``classname.name``。"""
    from helpcode.eval.driver import normalize_nodeid

    assert normalize_nodeid("tests/test_a.py::test_x") == ("test_a.py", "test_x")
    assert normalize_nodeid("tests.test_a.test_x") == ("test_a.py", "test_x")


def test_parse_junit(tmp_path):
    xml = """<?xml version="1.0"?>
<testsuites><testsuite>
<testcase classname="tests.test_a" name="test_ok"/>
<testcase classname="tests.test_a" name="test_bad"><failure message="x"/></testcase>
</testsuite></testsuites>"""
    path = tmp_path / "junit.xml"
    path.write_text(xml, encoding="utf-8")
    from helpcode.eval.driver import parse_junit

    results = parse_junit(path)
    assert results[("test_a.py", "test_ok")] is True
    assert results[("test_a.py", "test_bad")] is False


def test_pytest_command_substitutes_python():
    """``{python}`` 必须换成当前解释器——评测机的 python 未必装了 pytest。"""
    import sys

    from helpcode.eval.driver import _pytest_command

    cmd = _pytest_command(["{python} -m pytest"], Path("/tmp/j.xml"))
    assert sys.executable in cmd
    assert "--junitxml=" in cmd


# ---------------------------------------------------------------- 端到端（不调模型）


def test_local_driver_gold_resolves(tmp_path):
    """gold 修复 → 判定 resolved。

    这是「先跑 gold 定分母」的本地版：判定链路（注入 test_patch → 跑 pytest
    → 解析 junit → 比对 F2P/P2P）必须能自己验证自己。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    git_root = make_repo(repo)  # 已含一次 base commit

    test_patch = (
        "diff --git a/tests/test_mod.py b/tests/test_mod.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_mod.py\n"
        "@@ -0,0 +1,5 @@\n"
        "+from mod import f\n"
        "+\n"
        "+\n"
        "+def test_f():\n"
        "+    assert f() == 2\n"
    )
    inst = Instance(
        instance_id="local__one",
        repo="",
        base_commit="HEAD",
        problem_statement="f 应该返回 2",
        test_patch=test_patch,
        fail_to_pass=["tests/test_mod.py::test_f"],
        test_cmds=["{python} -m pytest"],
        local_repo=str(git_root),
    )
    driver = LocalDriver(timeout=120)
    ws = driver.prepare(inst, tmp_path / "ws")
    # 修复
    (Path(ws) / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    patch = collect_patch(Path(ws))
    verdict = judge(inst, driver.run_tests(inst, Path(ws)), patch)
    assert verdict.resolved, verdict.detail


def test_local_driver_broken_syntax_is_not_env_error(tmp_path):
    """模型改坏语法 → pytest 退出码 2，但这是「没修好」，不是环境错误。

    若归成 judge_error，会被 report 从分母里剔除，等于给出虚高的解决率。
    """
    repo = tmp_path / "repo2"
    repo.mkdir()
    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git_root = make_repo(repo)  # 已含一次 base commit
    test_patch = (
        "diff --git a/tests/test_mod.py b/tests/test_mod.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_mod.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+from mod import f\n"
        "+def test_f():\n"
        "+    assert f()\n"
    )
    inst = Instance(
        instance_id="local__broken",
        repo="",
        base_commit="HEAD",
        problem_statement="p",
        test_patch=test_patch,
        fail_to_pass=["tests/test_mod.py::test_f"],
        test_cmds=["{python} -m pytest"],
        local_repo=str(git_root),
    )
    driver = LocalDriver(timeout=120)
    ws = driver.prepare(inst, tmp_path / "ws2")
    (Path(ws) / "mod.py").write_text("def f(:\n", encoding="utf-8")
    verdict = judge(inst, driver.run_tests(inst, Path(ws)), collect_patch(Path(ws)))
    assert not verdict.resolved
    assert verdict.reason == F2P_FAILED


# ---------------------------------------------------------------- 报告


def test_summarize_excludes_env_failures():
    rows = [
        {"instance_id": "a", "resolved": True, "reason": "resolved"},
        {"instance_id": "b", "resolved": False, "reason": "f2p_failed"},
        {"instance_id": "c", "resolved": False, "reason": "judge_error"},
    ]
    s = summarize(rows)
    assert s["total"] == 3 and s["resolved"] == 1
    assert s["env_failed"] == 1 and s["valid"] == 2
    assert s["resolved_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert s["resolved_rate_valid"] == pytest.approx(0.5)


def test_summarize_pass_metrics():
    """pass@1 = 通过 trial / 总 trial；pass^3 = 3 次全过的题占比。"""
    rows = [
        {
            "instance_id": "a",
            "resolved": True,
            "reason": "resolved",
            "trial_resolved": [True, True, True],
        },
        {
            "instance_id": "b",
            "resolved": True,
            "reason": "resolved",
            "trial_resolved": [True, False, False],
        },
        {
            "instance_id": "c",
            "resolved": False,
            "reason": "f2p_failed",
            "trial_resolved": [False, False, False],
        },
    ]
    s = summarize(rows)
    # 4 个通过 trial / 9 个总 trial
    assert s["pass_at_1"] == pytest.approx(4 / 9, abs=1e-4)
    # 只有 a 是 3 次全过
    assert s["pass_pow_3"] == pytest.approx(1 / 3, abs=1e-4)


# ---------------------------------------------------------------- 题集


def test_load_instances_from_file(tmp_path):
    path = tmp_path / "d.jsonl"
    rows = [
        {"instance_id": "a", "problem_statement": "p", "local_repo": "r"},
        {"instance_id": "b", "problem_statement": "q", "local_repo": "r"},
    ]
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    assert len(load_instances(str(path))) == 2
    assert [i.instance_id for i in load_instances(str(path), limit=1)] == ["a"]


def test_load_instances_missing_file():
    with pytest.raises(DatasetError):
        load_instances("no/such/file.jsonl")
