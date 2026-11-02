"""执行后端：题目在哪儿跑、测试怎么跑。

抽象成一个 ``Driver`` 而不是写死 Docker，原因有三个：

1. 没装 Docker 的机器（比如当前这台）也要能验证整条链路——用本地后端跑
   合成题，判定逻辑完全一致；
2. 官方镜像要求「单实例 4 CPU / 16 GB」，本地跑小样本比拉几十 GB 镜像现实；
3. 能把「执行环境」和「评测逻辑」分开——这是两件事。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .instance import Instance
from .patch import PatchError, apply_patch

JUNIT_DIR = ".helpcode-eval"
JUNIT_NAME = "junit.xml"


@dataclass
class TestOutcome:
    """一次测试执行的结果。``results`` 是归一化 nodeid → 是否通过。"""

    # 名字以 Test 开头，pytest 会当成测试类来收集——显式排除
    __test__ = False

    results: dict[str, bool] = field(default_factory=dict)
    stdout: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def passed(self, nodeid: str) -> bool:
        return self.results.get(normalize_nodeid(nodeid), False)


class Driver(Protocol):
    """执行后端协议。"""

    name: str

    def prepare(self, inst: Instance, dest: Path) -> Path: ...

    def run_tests(
        self, inst: Instance, workspace: Path, *, apply_test_patch: bool = True
    ) -> TestOutcome: ...


def normalize_nodeid(nodeid: str) -> tuple[str, str]:
    """把 nodeid 归一化成 ``(文件名, 测试名)``，跨 pytest / junit 两种写法匹配。

    - 数据集里：``tests/test_parse.py::test_a``
    - junit 里：``classname="tests.test_parse" name="test_a"``

    只比文件名 + 测试名，不比目录层级：同一仓库里同名测试文件极少，
    而目录前缀在不同调用方式下会变（``tests/`` vs ``./tests/``）。
    """
    text = nodeid.strip()
    if "::" in text:
        file_part, _, test_part = text.partition("::")
        return (Path(file_part).name, test_part)
    if "/" in text or text.endswith(".py"):
        return (Path(text).name, "")
    # junit 的 classname.name 形式：最后一段是文件名（点换成下划线前的原样）
    parts = text.split(".")
    if len(parts) >= 2:
        return (f"{parts[-2]}.py", parts[-1])
    return (text, "")


def _pytest_command(cmds: list[str], junit_path: Path) -> str:
    """给 pytest 命令补上 junit 输出；非 pytest 命令原样返回。

    ``{python}`` 占位符换成当前解释器——评测机的 ``python`` 未必是装了
    pytest 的那个（Windows 上还可能是应用商店的假 python）。
    """
    base = (cmds[0] if cmds else "{python} -m pytest").replace(
        "{python}", f'"{sys.executable}"'
    )
    if "pytest" not in base and "py.test" not in base:
        return base
    return f"{base} -p no:cacheprovider -q --junitxml={junit_path.as_posix()}"


def parse_junit(path: Path) -> dict[str, bool]:
    """解析 junit xml → nodeid 键的结果表。文件不存在返回空表。"""
    if not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {}
    results: dict[str, bool] = {}
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        key = (
            normalize_nodeid(f"{classname}.{name}")
            if classname
            else normalize_nodeid(name)
        )
        failed = case.find("failure") is not None or case.find("error") is not None
        results[key] = not failed
    return results


class LocalDriver:
    """本地执行：题目仓库直接在工作目录里跑（不需要 Docker）。

    只适合两类题：本地合成题，或依赖本来就装好的仓库。
    官方 SWE-bench-Live 的题必须用 ``DockerDriver``——它们的依赖在镜像里。
    """

    name = "local"

    def __init__(self, timeout: float = 600.0) -> None:
        self.timeout = timeout

    def prepare(self, inst: Instance, dest: Path) -> Path:
        workspace = (dest / inst.instance_id).resolve()
        _force_rmtree(workspace)
        if inst.is_local:
            shutil.copytree(inst.local_repo, workspace)
            return workspace
        if not inst.repo:
            raise PatchError(f"实例 {inst.instance_id} 既没有 local_repo 也没有 repo")
        clone = subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                f"https://github.com/{inst.repo}.git",
                str(workspace),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if clone.returncode != 0:
            raise PatchError(f"克隆失败：{clone.stderr.strip()[:300]}")
        checkout = subprocess.run(
            ["git", "-C", str(workspace), "checkout", "--quiet", inst.base_commit],
            check=False,
            capture_output=True,
            text=True,
        )
        if checkout.returncode != 0:
            raise PatchError(f"checkout {inst.base_commit} 失败")
        return workspace

    def run_tests(
        self, inst: Instance, workspace: Path, *, apply_test_patch: bool = True
    ) -> TestOutcome:
        if apply_test_patch and inst.test_patch:
            try:
                apply_patch(workspace, inst.test_patch)
            except PatchError as e:
                return TestOutcome(error=f"test_patch 应用失败：{e}")

        junit = workspace / JUNIT_DIR / JUNIT_NAME
        junit.parent.mkdir(parents=True, exist_ok=True)
        if junit.exists():
            junit.unlink()

        cmd = _pytest_command(inst.test_cmds, junit)
        proc = subprocess.run(
            cmd,
            cwd=workspace,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
        )
        results = parse_junit(junit)
        if not results:
            # 没有 junit：只能按退出码判断，粗粒度但比「什么都没跑」强
            results = {normalize_nodeid("__all__"): proc.returncode == 0}
            error = "" if proc.returncode == 0 else f"测试命令退出码 {proc.returncode}"
        else:
            # 有 junit 就不看退出码：pytest 的 2 常常是「模型把代码改到 import 不了」
            # 造成的收集错误，那是模型没修好，不是环境坏了——不能排除出分母。
            error = ""
        return TestOutcome(
            results=results,
            stdout=(proc.stdout or "")[-8000:],
            error=error,
        )


class DockerDriver:
    """容器执行：一题一镜像，依赖与隔离都由镜像保证。

    与官方 harness 的差别只在「谁打补丁、谁跑命令」——判定逻辑与
    ``LocalDriver`` 完全共用（同一份 ``TestOutcome`` / ``judge``）。
    """

    name = "docker"

    def __init__(self, timeout: float = 1800.0, docker: str = "docker") -> None:
        self.timeout = timeout
        self.docker = docker

    def prepare(self, inst: Instance, dest: Path) -> Path:
        # 容器后端也需要一个宿主目录来挂载（放 patch、junit、日志）
        workspace = (dest / inst.instance_id).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def run_tests(
        self, inst: Instance, workspace: Path, *, apply_test_patch: bool = True
    ) -> TestOutcome:
        image = inst.image_key or _image_name(inst.instance_id)
        junit_rel = f"{JUNIT_DIR}/{JUNIT_NAME}"
        script = [
            "set -e",
            "cd /testbed",
        ]
        if apply_test_patch and inst.test_patch:
            (workspace / "test.patch").write_text(inst.test_patch, encoding="utf-8")
            script.append("git apply --whitespace=nowarn /eval/test.patch")
        cmd = _pytest_command(inst.test_cmds, Path("/testbed") / junit_rel)
        script.append(cmd)
        proc = subprocess.run(
            [
                self.docker,
                "run",
                "--rm",
                "-v",
                f"{workspace}:/eval",
                "-w",
                "/testbed",
                "--entrypoint",
                "bash",
                image,
                "-lc",
                " && ".join(script),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
        )
        results = parse_junit(workspace / junit_rel)
        if not results:
            results = {normalize_nodeid("__all__"): proc.returncode == 0}
            error = "" if proc.returncode == 0 else f"容器退出码 {proc.returncode}"
        else:
            error = ""  # 同本地后端：有 junit 就不拿退出码当环境错误
        return TestOutcome(
            results=results,
            stdout=(proc.stdout or "")[-8000:],
            error=error,
        )


def _image_name(instance_id: str, platform: str = "linux") -> str:
    """官方镜像命名：``starryzhang/sweb.eval.x86_64.<iid>``，``__`` 换成 ``_1776_``。"""
    med = "x86_64" if platform == "linux" else "win"
    name = instance_id.replace("__", "_1776_").lower()
    return f"starryzhang/sweb.eval.{med}.{name}"


def _force_rmtree(path: Path) -> None:
    """删目录，删不掉就明确报错。

    ``shutil.rmtree(ignore_errors=True)`` 在 Windows 上很危险：git 对象文件带
    只读属性、pytest 还会留下句柄，删失败它一声不响，接着 ``copytree`` 抛一个
    「目录已存在」的错，排查方向全被带偏。这里先摘掉只读位，删完再验一次。
    """
    if not path.exists():
        return
    for item in [path, *path.rglob("*")]:
        try:
            item.chmod(0o700)
        except OSError:
            continue  # 个别条目没权限就算了，整体能否删掉由下面的校验决定
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        raise PatchError(f"工作目录删不掉（可能被占用）：{path}")


def make_driver(kind: str, timeout: float = 600.0) -> Driver:
    """按名字构造后端；未知名字直接报错，不静默降级。"""
    if kind == "local":
        return LocalDriver(timeout=timeout)
    if kind == "docker":
        return DockerDriver(timeout=timeout)
    raise ValueError(f"未知执行后端：{kind}（可选 local / docker）")
