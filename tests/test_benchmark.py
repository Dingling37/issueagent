import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from issue_agent.benchmark.runner import BenchmarkCase, BenchmarkRunner
from issue_agent.core.state import AgentState


class FakeAgent:
    def __init__(self, workspace, hidden_name="SecretTest.java", *, completed=True, verified=True):
        self.workspace = workspace
        self.hidden_name = hidden_name
        self.completed = completed
        self.verified = verified
        self.state = AgentState("task", str(workspace.root))
        self.result = {}
        self.metrics = {"llm_calls": 2, "tool_calls": 1, "token_usage": {"input": 10, "output": 5},
                        "elapsed_seconds": 0.1, "tests": 1}

    def run(self):
        # This assertion runs while Agent owns the workspace, before Judge exists.
        self.state.status = "failed" if any(self.workspace.root.rglob(self.hidden_name)) else (
            "completed" if self.completed else "failed")
        self.state.verification_results = [{"passed": self.verified}]
        return self.state


class FakeTestRunner:
    next_result = {"passed": True, "tests": 1, "skipped": 0, "failures": 0, "errors": 0}

    def __init__(self, workspace, state, tracer, changes, timeout):
        self.workspace = workspace

    def run(self):
        result = dict(self.next_result)
        result["hidden_present"] = any(self.workspace.root.rglob("SecretTest.java"))
        if not result["hidden_present"]:
            result["passed"] = False
        return result


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "pom.xml").write_text("<project/>", encoding="utf-8")
        (self.source / "App.java").write_text("class App {}", encoding="utf-8")
        subprocess.run(["git", "init", "--quiet", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.source), "-c", "user.name=Test",
                        "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "baseline"], check=True)
        self.commit = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        self.hidden = self.root / "private-hidden"
        self.hidden.mkdir()
        (self.hidden / "SecretTest.java").write_text("class SecretTest {}", encoding="utf-8")
        self.output = self.root / "runs"

    def case(self):
        return BenchmarkCase("case", self.source, self.commit, "change App", self.hidden,
                             "src/test/java/hidden")

    def runner(self, **agent_options):
        return BenchmarkRunner(self.output,
            lambda workspace, issue: FakeAgent(workspace, **agent_options),
            test_runner_factory=FakeTestRunner)

    def test_hidden_tests_are_injected_only_after_agent_stops(self):
        result = self.runner().run(self.case())
        self.assertTrue(result["agent_completed"])
        self.assertTrue(result["hidden_tests_passed"])
        self.assertTrue(result["hidden_test_result"]["hidden_present"])
        self.assertFalse(any(Path(result["agent_workspace"]).rglob("SecretTest.java")))

    def test_repeated_runs_start_from_same_commit_in_distinct_copies(self):
        first = self.runner().run(self.case())
        (Path(first["agent_workspace"]) / "App.java").write_text("changed")
        second = self.runner().run(self.case())
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["baseline_commit"], self.commit)
        self.assertEqual((Path(second["agent_workspace"]) / "App.java").read_text(), "class App {}")

    def test_verifier_false_positive_is_recorded(self):
        FakeTestRunner.next_result = {"passed": False, "tests": 1, "skipped": 0,
                                      "failures": 1, "errors": 0}
        self.addCleanup(setattr, FakeTestRunner, "next_result",
                        {"passed": True, "tests": 1, "skipped": 0, "failures": 0, "errors": 0})
        result = self.runner(verified=True).run(self.case())
        self.assertTrue(result["verifier_passed"])
        self.assertFalse(result["hidden_tests_passed"])
        self.assertTrue(result["verifier_false_positive"])

    def test_zero_or_all_skipped_hidden_tests_never_pass(self):
        for test_result in (
            {"passed": True, "tests": 0, "skipped": 0, "failures": 0, "errors": 0},
            {"passed": True, "tests": 3, "skipped": 3, "failures": 0, "errors": 0},
            {"passed": False, "tests": 0, "skipped": 0, "failures": 0, "errors": 0,
             "reason": "OSError: test process failed"},
        ):
            with self.subTest(test_result=test_result):
                FakeTestRunner.next_result = test_result
                result = self.runner().run(self.case())
                self.assertFalse(result["hidden_tests_passed"])
                self.assertTrue(result["verifier_false_positive"])
        FakeTestRunner.next_result = {"passed": True, "tests": 1, "skipped": 0,
                                      "failures": 0, "errors": 0}

    def test_hidden_tests_inside_source_are_rejected(self):
        inside = self.source / "hidden"
        inside.mkdir()
        (inside / "SecretTest.java").write_text("secret")
        case = BenchmarkCase("bad", self.source, self.commit, "task", inside, "tests/hidden")
        with self.assertRaisesRegex(ValueError, "outside source_repo"):
            self.runner().run(case)

    def test_structured_result_keeps_agent_metrics(self):
        result = self.runner().run(self.case())
        saved = json.loads((self.output / result["run_id"] / "benchmark-result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["agent_metrics"]["llm_calls"], 2)
        for key in ("agent_completed", "verifier_passed", "hidden_tests_passed",
                    "verifier_false_positive"):
            self.assertIn(key, saved)


if __name__ == "__main__":
    unittest.main()
