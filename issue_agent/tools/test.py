from ..core.recovery import RecoveryError
import json
import locale
import os
from pathlib import Path
import shutil
import signal
import subprocess
from time import monotonic, sleep
import xml.etree.ElementTree as ET
from ..core.state import AgentState
from ..trace.tracer import Tracer
from ..workspace.workspace import Workspace
from ..workspace.changes import RepositoryChanges


def decode_test_output(data: bytes) -> str:
    """优先读取 UTF-8；Windows 旧构建工具可能输出控制台/OEM 编码。

    仅用于可读摘要，磁盘上的原始日志字节保持不变。混合编码无法完全自动识别。
    """
    encodings = ["utf-8"]
    if os.name == "nt":
        import ctypes
        console_cp = ctypes.windll.kernel32.GetConsoleOutputCP()
        oem_cp = ctypes.windll.kernel32.GetOEMCP()
        encodings.extend(f"cp{cp}" for cp in (console_cp, oem_cp) if cp)
    encodings.append(locale.getpreferredencoding(False))
    for encoding in dict.fromkeys(encodings):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class TestRunner:
    """只运行程序选定的测试命令，模型不能传入 shell、参数或工作目录。"""
    gradle_no_daemon = False

    def __init__(self, workspace: Workspace, state: AgentState, tracer: Tracer,
                 changes: RepositoryChanges, timeout: int = 300, *, gradle_no_daemon: bool = False) -> None:
        if not 1 <= timeout <= 1800:
            raise ValueError("Test timeout must be between 1 and 1800 seconds")
        self.workspace, self.state, self.tracer = workspace, state, tracer
        self.changes, self.timeout = changes, timeout
        self.gradle_no_daemon = gradle_no_daemon
        self.on_complete = None

    def command(self) -> list[str]:
        windows = os.name == "nt"
        if self.workspace.resolve("pom.xml").is_file():
            wrapper = "mvnw.cmd" if windows else "mvnw"
            executable = str(self.workspace.resolve(wrapper)) if self.workspace.resolve(wrapper).is_file() else shutil.which("mvn")
            arguments = ["-B", "-DskipTests=false", "-Dmaven.test.skip=false", "test"]
        elif any(self.workspace.resolve(p).is_file() for p in ("build.gradle", "build.gradle.kts")):
            wrapper = "gradlew.bat" if windows else "gradlew"
            executable = str(self.workspace.resolve(wrapper)) if self.workspace.resolve(wrapper).is_file() else shutil.which("gradle")
            # 强制执行，不能把 UP-TO-DATE 或旧缓存报告当成本次验证。
            arguments = ["test"]
            # Agent 允许复用 daemon；Hidden Judge 显式禁用，仍强制真实执行测试。
            if self.gradle_no_daemon:
                arguments.append("--no-daemon")
            arguments.extend(["--rerun-tasks", "--no-build-cache"])
        else:
            raise ValueError("No pom.xml or build.gradle(.kts) at repository root")
        if not executable:
            raise ValueError(f"Build tool missing: install it on PATH or provide {wrapper}")
        if windows and Path(executable).suffix.lower() in (".cmd", ".bat"):
            # Windows 的 bat/cmd 需要 cmd.exe；只拼接已确定的路径和常量参数。
            if any(c in executable for c in '&|<>^%!"\r\n'):
                raise ValueError("Unsupported special character in build executable path")
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c",
                    '""' + executable + '" ' + " ".join(arguments) + '"']
        if not windows and executable == str(self.workspace.root / wrapper):
            return ["/bin/sh", executable, *arguments]
        return [executable, *arguments]

    def reports(self) -> dict[str, tuple[int, int]]:
        found = {}
        for directory, dirs, names in os.walk(self.workspace.root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in {".git", ".task_outputs", ".venv", "node_modules"}
                       and not (Path(directory) / d).is_symlink()
                       and not (hasattr(Path(directory) / d, "is_junction") and (Path(directory) / d).is_junction())]
            for name in names:
                relative = (Path(directory) / name).relative_to(self.workspace.root).as_posix()
                if name.startswith("TEST-") and name.endswith(".xml") and (
                    "/surefire-reports/" in "/" + relative or "/test-results/test/" in "/" + relative):
                    path = self.workspace.resolve(relative)
                    stat = path.stat()
                    found[relative] = (stat.st_mtime_ns, stat.st_size)
        return found

    @staticmethod
    def stop(process: subprocess.Popen) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    def run(self) -> dict:
        if getattr(self, "budget_guard", None):
            self.budget_guard.consume("test_runs")
        number = len(self.state.test_results) + 1
        log_relative = self.tracer.save(f"test-{number:03d}.log", "")
        log_path = self.workspace.resolve(log_relative, internal=True)
        started = monotonic()
        result = {"command": [], "passed": False, "returncode": None, "timed_out": False,
                  "tests": 0, "failures": 0, "errors": 0, "skipped": 0, "failure_details": [],
                  "output_path": log_relative, "fingerprint": "", "step": self.state.step_count}
        self.tracer.record("TEST_STARTED", self.state.step_count, test_number=number)
        try:
            result["command"] = self.command()
            result["fingerprint"] = self.changes.fingerprint()
            before = self.reports()
            # 日志直接写磁盘，避免数万行输出常驻内存。子进程不继承模型凭据。
            environment = {k: v for k, v in os.environ.items() if not any(
                marker in k.upper() for marker in ("API_KEY", "AUTH_TOKEN"))}
            launch: list[str] | str = result["command"]
            if os.name == "nt" and len(launch) >= 5 and launch[1:4] == ["/d", "/s", "/c"]:
                # cmd 使用自己的引号规则，不能让 list2cmdline 再次转义 /c 后的整段命令。
                launch = subprocess.list2cmdline(launch[:4]) + " " + launch[4]
            with log_path.open("wb") as log:
                process = subprocess.Popen(launch, cwd=self.workspace.root, env=environment,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt")
                try:
                    while process.poll() is None:
                        if getattr(self, "budget_guard", None):
                            self.budget_guard.check_runtime()
                        if monotonic() - started > self.timeout:
                            result["timed_out"] = True
                            self.stop(process)
                            break
                        if log_path.stat().st_size > 50_000_000:
                            self.stop(process)
                            raise ValueError("Test log exceeded 50 MB limit")
                        sleep(0.1)
                except BaseException:
                    self.stop(process)
                    raise
                result["returncode"] = process.returncode
            for relative, stamp in self.reports().items():
                if before.get(relative) == stamp:
                    continue  # 旧报告不能作为这次运行通过的证据。
                path = self.workspace.resolve(relative)
                if path.stat().st_size > 10_000_000:
                    raise ValueError("Test XML exceeds 10 MB limit")
                root = ET.fromstring(path.read_bytes())
                suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
                for suite in suites:
                    for key in ("tests", "failures", "errors", "skipped"):
                        result[key] += int(suite.get(key, "0"))
                    for case in suite.findall("testcase"):
                        failure = case.find("failure")
                        if failure is None:
                            failure = case.find("error")
                        if failure is not None and len(result["failure_details"]) < 10:
                            result["failure_details"].append((case.get("classname", "") + "." +
                                case.get("name", "") + ": " + failure.get("message", ""))[:700])
            result["passed"] = (result["returncode"] == 0 and not result["timed_out"]
                and result["tests"] > result["skipped"] and not result["failures"] and not result["errors"]
                and result["fingerprint"] == self.changes.fingerprint())
            if not result["passed"]:
                result["reason"] = "Test command failed, timed out, changed sources, or produced no fresh passing tests"
        except RecoveryError:
            raise
        except Exception as exc:
            result["reason"] = f"{type(exc).__name__}: {exc}"
        with log_path.open("rb") as stream:
            stream.seek(max(0, log_path.stat().st_size - 4000))
            result["output_tail"] = decode_test_output(stream.read(4000))
        result["duration"] = monotonic() - started
        self.state.test_results.append(result)
        self.tracer.record("TEST_PASSED" if result["passed"] else "TEST_FAILED",
                           self.state.step_count, duration=result["duration"], result=result)
        if self.on_complete:
            self.on_complete()
        return result

    def run_tool(self) -> str:
        result = self.run()
        if not result["passed"]:
            # 让 Registry 同时标记 is_error，模型看到的内容仍包含结构化摘要。
            raise ValueError(json.dumps(result, ensure_ascii=False))
        return json.dumps(result, ensure_ascii=False)
