from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
from time import monotonic
from typing import Any, Callable, Protocol
from uuid import uuid4

from ..core.state import AgentState
from ..tools.test import TestRunner
from ..trace.tracer import Tracer
from ..workspace.changes import RepositoryChanges
from ..workspace.workspace import Workspace


class AgentRun(Protocol):
    state: AgentState
    result: dict[str, Any]

    def run(self) -> AgentState: ...


@dataclass(frozen=True)
class BenchmarkCase:
    """One fixed task. Hidden tests live outside the repository supplied to Agent."""

    case_id: str
    source_repo: Path
    baseline_commit: str
    issue: str
    hidden_tests: Path
    hidden_destination: str


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise ValueError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


class BenchmarkRunner:
    """Run Agent first, then create a separate Judge copy and inject hidden tests."""

    def __init__(self, output_root: Path, agent_factory: Callable[[Workspace, str], AgentRun],
                 *, test_timeout: int = 300,
                 test_runner_factory: Callable[..., TestRunner] = TestRunner) -> None:
        self.output_root = output_root.resolve()
        self.agent_factory = agent_factory
        self.test_timeout = test_timeout
        self.test_runner_factory = test_runner_factory

    @staticmethod
    def _validate(case: BenchmarkCase) -> tuple[Path, Path, str]:
        source = case.source_repo.resolve(strict=True)
        hidden = case.hidden_tests.resolve(strict=True)
        if not source.is_dir() or not (source / ".git").exists():
            raise ValueError("source_repo must be a Git repository")
        if not hidden.is_dir() or not any(path.is_file() for path in hidden.rglob("*")):
            raise ValueError("hidden_tests must contain at least one file")
        if hidden == source or source in hidden.parents or hidden in source.parents:
            raise ValueError("hidden_tests must be outside source_repo")
        destination = Path(case.hidden_destination)
        if destination.is_absolute() or ".." in destination.parts or not destination.parts:
            raise ValueError("hidden_destination must stay inside Judge workspace")
        if destination.parts[:2] != ("src", "test"):
            raise ValueError("hidden_destination must be under src/test")
        commit = _git(source, "rev-parse", f"{case.baseline_commit}^{{commit}}")
        return source, hidden, commit

    @staticmethod
    def _clone_at(source: Path, commit: str, destination: Path) -> None:
        result = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(destination)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise ValueError(f"git clone failed: {result.stderr.strip()}")
        _git(destination, "checkout", "--quiet", "--detach", commit)

    @staticmethod
    def _copy_for_judge(agent_workspace: Path, judge_workspace: Path) -> None:
        ignored = shutil.ignore_patterns(".git", ".task_outputs", "target", "build", ".gradle")
        shutil.copytree(agent_workspace, judge_workspace, ignore=ignored)
        subprocess.run(["git", "init", "--quiet", str(judge_workspace)], check=True, capture_output=True)

    def run(self, case: BenchmarkCase) -> dict[str, Any]:
        source, hidden, commit = self._validate(case)
        run_id = f"{case.case_id}-{uuid4().hex}"
        run_dir = self.output_root / run_id
        agent_path, judge_path = run_dir / "agent-workspace", run_dir / "judge-workspace"
        run_dir.mkdir(parents=True, exist_ok=False)
        started = monotonic()
        result: dict[str, Any] = {
            "schema_version": 1, "run_id": run_id, "case_id": case.case_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "baseline_commit": commit, "agent_workspace": str(agent_path),
            "judge_workspace": str(judge_path), "agent_completed": False,
            "verifier_passed": False, "hidden_tests_passed": False,
            "verifier_false_positive": False, "agent_status": "not_started",
            "agent_error": None, "agent_metrics": {}, "hidden_test_result": None,
        }
        try:
            # At this point hidden files exist only at `hidden`, outside the cloned workspace.
            self._clone_at(source, commit, agent_path)
            agent_workspace = Workspace(agent_path)
            agent = self.agent_factory(agent_workspace, case.issue)
            state = agent.run()
            result["agent_status"], result["agent_error"] = state.status, state.error
            result["agent_completed"] = state.status == "completed"
            result["verifier_passed"] = bool(
                state.verification_results and state.verification_results[-1].get("passed"))
            result["agent_metrics"] = dict(getattr(agent, "metrics", {}) or {})

            # Judge is created only after Agent.run() returns. Agent never receives this path.
            self._copy_for_judge(agent_path, judge_path)
            judge_workspace = Workspace(judge_path)
            # Judge executes hidden tests only. Otherwise visible tests could make a missing or
            # undiscovered hidden suite look non-empty and produce a false success.
            visible_tests = judge_workspace.resolve("src/test")
            if visible_tests.exists():
                shutil.rmtree(visible_tests)
            destination = judge_workspace.resolve(case.hidden_destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(hidden, destination)

            judge_state = AgentState(task=f"Hidden tests for {case.case_id}", repo_path=str(judge_path))
            judge_trace = Tracer(judge_workspace)
            judge_runner = self.test_runner_factory(judge_workspace, judge_state, judge_trace,
                                                     RepositoryChanges(judge_workspace), self.test_timeout,
                                                     gradle_no_daemon=True)
            hidden_result = judge_runner.run()
            result["hidden_test_result"] = hidden_result
            # Keep the benchmark invariant explicit even if a future runner implementation
            # accidentally reports passed=True for an empty/all-skipped suite.
            tests = int(hidden_result.get("tests", 0))
            skipped = int(hidden_result.get("skipped", 0))
            result["hidden_tests_passed"] = bool(
                hidden_result.get("passed") and tests > 0 and tests > skipped
                and not hidden_result.get("failures", 0) and not hidden_result.get("errors", 0)
                and not hidden_result.get("timed_out", False))
            result["judge_outputs"] = str(judge_trace.directory)
        except Exception as exc:
            result["benchmark_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            result["verifier_false_positive"] = bool(
                result["verifier_passed"] and not result["hidden_tests_passed"])
            result["elapsed_seconds"] = monotonic() - started
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "benchmark-result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
