"""执行后端：题目在哪儿跑、测试怎么跑。

抽象成一个 ``Driver`` 而不是写死 Docker，原因有三个：

1. 没装 Docker 的机器（比如当前这台）也要能验证整条链路——用本地后端跑
   合成题，判定逻辑完全一致；
2. 官方镜像要求「单实例 4 CPU / 16 GB」，本地跑小样本比拉几十 GB 镜像现实；
3. 能把「执行环境」和「评测逻辑」分开——这是两件事。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .instance import Instance
from .patch import PatchError, apply_patch, changed_files

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
        file_part, _, rest = text.partition("::")
        # ``tests/test_x.py::TestClass::test_method`` 里只保留最后一个 ``::``
        # 之后的测试函数名，忽略中间的类名——junit 侧只有函数名。
        test_name = rest.rsplit("::", 1)[-1]
        return (Path(file_part).name, test_name)
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
    # 裸 ``pytest`` / ``py.test`` 命令替换成当前 venv 的解释器 ``-m pytest``。
    # 评测机 shell 的 PATH 里可能挂着 conda/系统里的旧 pytest（比如 conda 的
    # 6.x），它用的是另一个 Python，收集不到 venv 里装的依赖 → 退出码 4。
    base = re.sub(r"^(pytest|py\.test)\b", lambda _m: f'"{sys.executable}" -m pytest', base)
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
        if classname:
            # classname 形如 ``tests.test_x.TestClass`` 或 ``tests.test_x``。
            # 模块名是 ``test``/``tests`` 包之后的第一个段（有类名时）；找不到
            # 就退回倒数第二段。文件名统一补 ``.py``，与数据集侧对齐。
            cn = classname.split(".")
            module = None
            for idx, seg in enumerate(cn):
                if seg in ("test", "tests") and idx + 1 < len(cn):
                    module = cn[idx + 1]
                    break
            if module is None:
                module = cn[-2] if len(cn) >= 2 else cn[-1]
            key = (f"{module}.py", name)
        else:
            key = normalize_nodeid(name)
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
            # agent 可能误改了 test_patch 涉及的文件（弱模型很常见：把要加
            # 的断言也写进测试文件），导致 apply 时上下文冲突 → 误判成
            # judge_error。先把这些文件恢复到 base_commit 再 apply，让「改了
            # 测试文件」这种错误走正常的 f2p 判定，而不是崩成环境错误。
            for f in changed_files(inst.test_patch):
                subprocess.run(
                    ["git", "-C", str(workspace), "checkout", "--", f],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
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
    「目录已存在」的错，排查方向全被带偏。

    这里先摘只读位，再带退避重试：Windows 上 pytest / git / 杀软可能在进程
    结束后仍短暂持有句柄（毫秒级），一次 ``rmtree`` 删不干净会导致下一 trial
    的 ``prepare`` 直接失败——这不是模型能力问题，是环境时序问题。重试几次
    通常就能删掉，只有多次重试仍失败才报错。
    """
    if not path.exists():
        return
    last_err: OSError | None = None
    for attempt in range(8):
        for item in [path, *path.rglob("*")]:
            try:
                item.chmod(0o700)
            except OSError:
                continue  # 个别条目没权限就算了，整体能否删掉由下面的校验决定
        try:
            shutil.rmtree(path)
        except OSError as e:  # Windows 句柄占用会抛 PermissionError
            last_err = e
        if not path.exists():
            return
        # 递增退避：0.2s / 0.4s / ... / 1.4s，给句柄释放留时间
        time.sleep(0.2 * (attempt + 1))
    # 兜底：agent 在 Git Bash 里执行 Windows 重定向（如 `> nul`）会造出名为
    # nul / con 等「保留设备名」的文件。这类文件 Win32 API（os.remove / rmtree）
    # 永远删不掉——路径末尾的 nul 会被解析成 NUL 设备而不是文件。用 cmd 的 rd
    # 加 ``\\?\`` 前缀绕过设备名解析，递归删掉整个目录。
    if sys.platform == "win32":
        _rmtree_win32(path)
        if not path.exists():
            return
    raise PatchError(
        f"工作目录删不掉（可能被占用）：{path}"
        + (f"（{last_err}）" if last_err else "")
    )


def _rmtree_win32(path: Path) -> None:
    """Windows 兜底删除：``cmd /c rd /s /q "\\?\\<绝对路径>"``。

    ``\\?\\`` 前缀关闭 Win32 路径解析（含保留设备名劫持），``rd /s /q`` 递归
    静默删除。只处理 ``nul`` 这类常规 API 删不掉的残留，正常目录在重试阶段
    就已经被 ``shutil.rmtree`` 清掉了。
    """
    win_path = "\\\\?\\" + str(path.resolve()).replace("/", "\\")
    subprocess.run(
        ["cmd", "/c", "rd", "/s", "/q", win_path],
        check=False,
        capture_output=True,
    )


def make_driver(kind: str, timeout: float = 600.0) -> Driver:
    """按名字构造后端；未知名字直接报错，不静默降级。"""
    if kind == "local":
        return LocalDriver(timeout=timeout)
    if kind == "docker":
        return DockerDriver(timeout=timeout)
    raise ValueError(f"未知执行后端：{kind}（可选 local / docker）")
