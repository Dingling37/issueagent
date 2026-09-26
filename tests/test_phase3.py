import json
import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from issue_agent.core.agent import Agent
from issue_agent.core.model import ModelResponse, ScriptedModelClient
from issue_agent.core.state import AgentState
from issue_agent.core.structured import json_object
from issue_agent.tools.test import TestRunner, decode_test_output
from issue_agent.tools.git import GitTools
from issue_agent.trace.tracer import Tracer
from issue_agent.verification.verifier import IssueVerifier
from issue_agent.workspace.workspace import Workspace
from issue_agent.workspace.changes import RepositoryChanges
from issue_agent.planning.planner import Planner


def decision(completed=True, missing=None) -> ModelResponse:
    return ModelResponse([{"type": "tool_use", "id": "review-1", "name": "submit_verification",
        "input": {"completed": completed, "reason": "Checked the issue, diff and tests",
                  "missing_requirements": missing or []}}], stop_reason="tool_use")


def tool(name: str, **args) -> ModelResponse:
    return ModelResponse([{"type": "tool_use", "id": "call-1", "name": name, "input": args}])


class Phase3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "Example.java").write_text("class Example {}", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.ws = Workspace(self.root)
        self.state = AgentState("Add validation and tests", str(self.root))
        self.trace = Tracer(self.ws)
        self.changes = RepositoryChanges(self.ws)
        self.runner = TestRunner(self.ws, self.state, self.trace, self.changes, timeout=2)

    def report_command(self, failed=False, skipped=False) -> list[str]:
        # 只在测试中替换命令，生产工具完全不接受这类自定义命令。
        xml = ('<testsuite tests="1" failures="%s" errors="0" skipped="%s">'
               '<testcase classname="ExampleTest" name="rejectsInvalid">%s</testcase></testsuite>') % (
                   int(failed), int(skipped), '<failure message="invalid accepted"/>' if failed else '')
        script = "from pathlib import Path; import sys; p=Path('target/surefire-reports'); p.mkdir(parents=True, exist_ok=True); (p/'TEST-demo.xml').write_text(" + repr(xml) + "); print('test output'); sys.exit(" + str(int(failed)) + ")"
        return [sys.executable, "-c", script]

    def change(self) -> None:
        (self.root / "Example.java").write_text("class Example { int value; }", encoding="utf-8")
        self.state.files_modified.append("Example.java")

    def verifier(self, review=None) -> IssueVerifier:
        return IssueVerifier(self.changes, self.runner, GitTools(self.ws), self.trace,
                             review or (lambda context, budget: decision()))

    def test_plan_contains_issue_build_and_status(self):
        plan = Planner().create(self.state, {"build_system": "maven"})
        self.assertIn(self.state.task, plan[0]["task"])
        self.assertIn("maven", plan[2]["task"])
        self.assertEqual([x["id"] for x in plan], [1, 2, 3, 4])

    def test_snapshot_includes_new_files_and_excludes_outputs(self):
        (self.root / "NewTest.java").write_text("new test")
        self.trace.save("noise.txt", "not a source change")
        names, diff = self.changes.diff()
        self.assertEqual(names, ["NewTest.java"])
        self.assertIn("/dev/null", diff)
        self.assertNotIn(".task_outputs", diff)

    def test_existing_dirty_files_do_not_count_as_task_changes(self):
        self.assertFalse(self.verifier().verify(self.state).passed)

    def test_test_report_success_and_failure(self):
        with patch.object(self.runner, "command", return_value=self.report_command(failed=True)):
            failed = self.runner.run()
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["failures"], 1)
        self.assertIn("rejectsInvalid", failed["failure_details"][0])
        with patch.object(self.runner, "command", return_value=self.report_command()):
            success = self.runner.run()
        self.assertTrue(success["passed"])
        self.assertEqual(success["tests"], 1)

    def test_no_test_stale_reports_and_all_skipped_are_rejected(self):
        with patch.object(self.runner, "command", return_value=[sys.executable, "-c", "pass"]):
            self.assertFalse(self.runner.run()["passed"])
        with patch.object(self.runner, "command", return_value=self.report_command()):
            self.assertTrue(self.runner.run()["passed"])
        with patch.object(self.runner, "command", return_value=[sys.executable, "-c", "pass"]):
            self.assertFalse(self.runner.run()["passed"])
        with patch.object(self.runner, "command", return_value=self.report_command(skipped=True)):
            self.assertFalse(self.runner.run()["passed"])

    def test_timeout_is_structured(self):
        self.runner.timeout = 1
        with patch.object(self.runner, "command", return_value=[sys.executable, "-c", "import time; time.sleep(30)"]):
            result = self.runner.run()
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["passed"])
        self.assertLess(result["duration"], 10)

    def test_missing_tool_is_clear(self):
        (self.root / "pom.xml").write_text("<project/>")
        with patch("issue_agent.tools.test.shutil.which", return_value=None):
            result = self.runner.run()
        self.assertFalse(result["passed"])
        self.assertIn("Build tool missing", result["reason"])

    def test_maven_and_gradle_command_selection(self):
        (self.root / "pom.xml").write_text("<project/>")
        with patch("issue_agent.tools.test.shutil.which", return_value="mvn"):
            command = self.runner.command()
        self.assertIn("test", command)
        (self.root / "pom.xml").unlink()
        (self.root / "build.gradle.kts").write_text("")
        with patch("issue_agent.tools.test.shutil.which", return_value="gradle"):
            command = self.runner.command()
        self.assertIn("--rerun-tasks", command)

    def test_verifier_reruns_after_edit_and_requires_valid_review(self):
        self.change()
        with patch.object(self.runner, "command", return_value=self.report_command()):
            self.runner.run()
            (self.root / "Example.java").write_text("class Example { int value = 1; }")
            self.assertTrue(self.verifier().verify(self.state).passed)
        self.assertEqual(len(self.state.test_results), 2)
        self.assertFalse(self.verifier(lambda context, budget: decision(True, ["Missing invalid test"])).verify(self.state).passed)
        bad = ModelResponse([{"type": "text", "text": '{"completed": "true"}'}])
        self.assertFalse(self.verifier(lambda context, budget: bad).verify(self.state).passed)

    def test_truncated_json_rejected(self):
        result = decision()
        result.stop_reason = "max_tokens"
        with self.assertRaises(ValueError):
            json_object(result)

    def review_agent(self, reviews):
        model = ScriptedModelClient([
            tool("edit_file", path="Example.java", old_text="class Example {}", new_text="class Example { int value; }"),
            ModelResponse([{"type": "text", "text": "Ready for review"}]),
            *reviews,
        ])
        agent = Agent(self.ws, "Add a field", model)
        with patch.object(agent.runner, "command", return_value=self.report_command()):
            agent.run()
        return agent, model

    def test_truncated_review_retries_internally_with_larger_budget(self):
        truncated = ModelResponse([{"type": "thinking", "thinking": "incomplete"}], 10, 4096, "max_tokens")
        agent, model = self.review_agent([truncated, decision()])
        self.assertEqual(agent.state.status, "completed", agent.state.error)
        self.assertEqual(model.token_limits, [4096, 4096, 4096, 8192])
        self.assertEqual(agent.state.step_count, 2)
        self.assertEqual(agent.state.verification_failures, 0)
        self.assertEqual(agent.state.verification_retries, 1)
        self.assertEqual(len(agent.state.test_results), 1)
        self.assertEqual(agent.state.token_usage["output"], 4096)
        self.assertNotIn("incomplete", json.dumps(model.contexts[-1]))
        self.assertIn("test_history", model.contexts[-1][0]["content"])
        events = (agent.tracer.directory / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("VERIFICATION_RETRY", events)

    def test_invalid_review_json_retries_without_budget_increase(self):
        agent, model = self.review_agent([ModelResponse([{"type": "text", "text": "not JSON"}]), decision()])
        self.assertEqual(agent.state.status, "completed")
        self.assertEqual(model.token_limits[-2:], [4096, 4096])

    def test_verification_tool_is_forced_and_not_exposed_to_executor(self):
        agent, model = self.review_agent([decision()])
        self.assertEqual(agent.state.status, "completed")
        self.assertEqual(model.tool_choices[:2], [None, None])
        self.assertEqual(model.tool_choices[2], {"type": "tool", "name": "submit_verification"})
        self.assertEqual([tool["name"] for tool in model.tool_schemas[2]], ["submit_verification"])
        self.assertNotIn("submit_verification", [tool["name"] for tool in model.tool_schemas[0]])
        self.assertEqual(len(agent.state.tool_history), 1)  # Submission never executes via Registry.
        self.assertEqual(agent.state.llm_calls, 3)

    def test_review_receives_snapshot_but_full_test_result_is_retained(self):
        agent, model = self.review_agent([decision()])
        self.assertEqual(agent.state.status, "completed")
        payload = json.loads(model.contexts[2][0]["content"])
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["original_issue"], "Add a field")
        self.assertEqual(payload["changed_files"], ["Example.java"])
        self.assertIn("int value", payload["final_diff"])
        self.assertTrue(all(payload["deterministic_checks"].values()))
        self.assertTrue(payload["final_test_result"]["passed"])
        self.assertNotIn(str(self.root), model.contexts[2][0]["content"])
        for key in ("command", "output_path", "fingerprint", "output_tail"):
            self.assertIn(key, agent.state.test_results[-1])
            self.assertNotIn(key, payload["final_test_result"])
            self.assertNotIn(key, payload["test_history"][-1])

    def test_bad_verification_submissions_retry_then_accept(self):
        valid = decision()
        invalid = []
        for field, value in (("completed", "true"), ("reason", " "), ("missing_requirements", "none")):
            response = copy.deepcopy(valid)
            response.content[0]["input"][field] = value
            invalid.append(response)
        for change in ("missing", "extra", "unknown", "multiple", "string_input", "missing_id", "wrong_stop"):
            response = copy.deepcopy(valid)
            if change == "missing": del response.content[0]["input"]["reason"]
            elif change == "extra": response.content[0]["input"]["unexpected"] = True
            elif change == "unknown": response.content[0]["name"] = "edit_file"
            elif change == "multiple": response.content.append(copy.deepcopy(response.content[0]))
            elif change == "string_input": response.content[0]["input"] = '{"completed":true}'
            elif change == "missing_id": del response.content[0]["id"]
            else: response.stop_reason = "end_turn"
            invalid.append(response)
        invalid.extend([ModelResponse([]), ModelResponse([{"type": "text", "text":
            json.dumps(valid.content[0]["input"])}])])  # Even valid text JSON is not a submission.
        self.change()
        with patch.object(self.runner, "command", return_value=self.report_command()):
            self.assertTrue(self.runner.run()["passed"])
        for response in invalid:
            with self.subTest(response=response):
                replies = iter([response, decision()])
                budgets = []
                def review(context, budget):
                    budgets.append(budget)
                    return next(replies)
                result = self.verifier(review).verify(self.state)
                self.assertTrue(result.passed, result.feedback)
                self.assertEqual(budgets, [4096, 4096])

    def test_truncated_tool_arguments_are_not_accepted(self):
        truncated = decision()
        truncated.stop_reason = "max_tokens"  # Even seemingly complete arguments cannot pass.
        agent, model = self.review_agent([truncated, decision()])
        self.assertEqual(agent.state.status, "completed")
        self.assertEqual(agent.state.verification_retries, 1)
        self.assertEqual(model.token_limits[-2:], [4096, 8192])

    def test_missing_submission_exhaustion_stops(self):
        text = ModelResponse([{"type": "text", "text": '{"completed": true}'}])
        agent, _ = self.review_agent([text, text])
        self.assertEqual(agent.state.status, "verification_error")
        self.assertIsNone(agent.state.pr_title)
        self.assertEqual(len(agent.state.tool_history), 1)

    def test_review_retry_exhaustion_stops_without_more_edits(self):
        truncated = ModelResponse([], output_tokens=4096, stop_reason="max_tokens")
        agent, model = self.review_agent([truncated, truncated])
        self.assertEqual(agent.state.status, "verification_error")
        self.assertIn("after 2 attempts", agent.state.error)
        self.assertIn("max_tokens", agent.state.error)
        self.assertEqual(agent.state.step_count, 2)
        self.assertEqual(agent.state.verification_failures, 0)
        self.assertEqual(len(agent.state.tool_history), 1)
        self.assertEqual(len(model.contexts), 4)
        self.assertNotIn("TASK_COMPLETED", (agent.tracer.directory / "events.jsonl").read_text(encoding="utf-8"))

    def test_review_api_error_does_not_trigger_code_repair(self):
        self.change()
        with patch.object(self.runner, "command", return_value=self.report_command()):
            self.runner.run()
        def broken_review(context, budget):
            raise ConnectionError("API unavailable")
        result = self.verifier(broken_review).verify(self.state)
        self.assertTrue(result.infrastructure_error)
        self.assertFalse(result.passed)
        self.assertIn("API unavailable", result.feedback)

    def test_success_exports_pr_and_shared_result(self):
        agent, model = self.review_agent([decision()])
        self.assertEqual(agent.state.status, "completed")
        self.assertEqual(agent.state.pr_title, "Add a field")
        self.assertEqual(agent.state.changed_files, ["Example.java"])
        self.assertIn("1 total, 1 passed, 0 failed", agent.state.pr_description)
        payload = json.loads((agent.tracer.directory / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(payload, agent.result)
        self.assertEqual(payload["metrics"]["llm_calls"], 3)  # 文案生成不增加 API 调用。
        for path in payload["artifacts"].values():
            self.assertTrue((self.root / path).exists())

    def test_verification_error_does_not_export_pr(self):
        response = ModelResponse([], stop_reason="max_tokens")
        agent, _ = self.review_agent([response, response])
        payload = json.loads((agent.tracer.directory / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "verification_error")
        self.assertIsNone(payload["pr_title"])
        self.assertFalse((agent.tracer.directory / "pr-description.md").exists())

    def test_pr_summary_uses_successful_review_not_executor_claim(self):
        review = ModelResponse([{"type": "tool_use", "id": "review-1", "name": "submit_verification", "input": {
            "completed": True, "reason": "Adds a value field.\nPreserves existing behavior.",
            "missing_requirements": []}}], stop_reason="tool_use")
        agent, _ = self.review_agent([review])
        description = agent.state.pr_description
        self.assertIn("> Adds a value field.\n> Preserves existing behavior.", description)
        self.assertNotIn("Ready for review", description)
        self.assertIn("1 total, 1 passed, 0 failed", description)
        self.assertEqual(agent.state.llm_calls, 3)
        self.assertEqual((agent.tracer.directory / "pr-description.md").read_text(encoding="utf-8"), description)

    def test_pr_summary_ignores_unstructured_or_failed_reviews(self):
        from issue_agent.delivery.result import ResultBuilder
        for feedback in ("Legacy review passed", "[]", '{"completed": false, "reason": "Missing tests"}',
                         '{"completed": true, "reason": null}'):
            with self.subTest(feedback=feedback):
                self.state.verification_results = [{"passed": True, "feedback": feedback}]
                self.assertIsNone(ResultBuilder.reviewed_summary(self.state))
        self.state.verification_results = [{"passed": False, "feedback":
            '{"completed": true, "reason": "Contradictory review"}'}]
        self.assertIsNone(ResultBuilder.reviewed_summary(self.state))

    def test_delivery_rejects_sources_changed_after_tests(self):
        from issue_agent.delivery.result import ResultBuilder
        agent, _ = self.review_agent([decision()])
        (self.root / "Example.java").write_text("changed after verification")
        with self.assertRaisesRegex(ValueError, "sources changed"):
            ResultBuilder(agent.changes, agent.tracer).build(agent.state)

    def test_real_verifier_feedback_reenters_agent(self):
        model = ScriptedModelClient([
            tool("edit_file", path="Example.java", old_text="class Example {}", new_text="class Example { int value; }"),
            ModelResponse([{"type": "text", "text": "Done"}]),
            decision(False, ["Need regression test"]),
            tool("create_file", path="ExampleTest.java", content="regression test"),
            ModelResponse([{"type": "text", "text": "Done"}]),
            decision(),
        ])
        agent = Agent(self.ws, "Add a field and a regression test", model)
        with patch.object(agent.runner, "command", return_value=self.report_command()):
            result = agent.run()
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.verification_failures, 1)
        self.assertEqual(len(result.test_results), 2)
        self.assertIn("Need regression test", json.dumps(model.contexts[3]))
        self.assertEqual(result.llm_calls, 6)

    def test_run_test_rejects_command_injection(self):
        agent = Agent(self.ws, "test", ScriptedModelClient([]))
        summary, ok = agent.registry.execute("run_test", {"command": "git reset --hard"})
        self.assertFalse(ok)
        self.assertIn("Expected exactly", summary)

    @unittest.skipUnless(os.name == "nt", "Windows batch quoting regression")
    def test_gradle_wrapper_with_space_path_and_fresh_xml(self):
        # 真正启动 .bat，验证 Windows 引号和 Gradle 报告目录，不冒充真实 Gradle 引擎。
        import ctypes
        kernel = ctypes.windll.kernel32
        # chcp 会影响共享控制台；测试结束（包括失败）后恢复调用者的设置。
        original_input, original_output = kernel.GetConsoleCP(), kernel.GetConsoleOutputCP()
        if original_input:
            self.addCleanup(kernel.SetConsoleCP, original_input)
        if original_output:
            self.addCleanup(kernel.SetConsoleOutputCP, original_output)
        for code_page in (936, 65001):
            with self.subTest(code_page=code_page):
                nested = self.root / f"中文 space repo {code_page}"
                nested.mkdir()
                (nested / "build.gradle").write_text("")
                script = self.report_command()[-1].replace("target/surefire-reports", "build/test-results/test")
                (nested / "emit_report.py").write_text(script, encoding="utf-8")
                # bat 只含 ASCII；Unicode 路径通过子进程环境传递，不受脚本代码页影响。
                (nested / "gradlew.bat").write_text(
                    f'@echo off\nchcp {code_page} >nul\n"%ISSUE_AGENT_TEST_PYTHON%" emit_report.py\n',
                    encoding="ascii")
                ws = Workspace(nested)
                runner = TestRunner(ws, AgentState("test", str(nested)), Tracer(ws), RepositoryChanges(ws))
                with patch.dict(os.environ, {"ISSUE_AGENT_TEST_PYTHON": sys.executable}):
                    result = runner.run()
                self.assertTrue(result["passed"], result)

    def test_utf8_log_summary(self):
        message = "系统找不到指定的路径。"
        self.assertEqual(decode_test_output(message.encode("utf-8")), message)

    @unittest.skipUnless(os.name == "nt", "Windows code page decoding")
    def test_windows_log_summary_preserves_raw_bytes(self):
        message = "系统找不到指定的路径。"
        raw = message.encode("gbk")
        command = [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({raw!r}); sys.exit(1)"]
        with patch("ctypes.windll.kernel32.GetConsoleOutputCP", return_value=936), \
                patch.object(self.runner, "command", return_value=command):
            result = self.runner.run()
        self.assertFalse(result["passed"])
        self.assertEqual(result["output_tail"], message)
        self.assertEqual((self.root / result["output_path"]).read_bytes(), raw)

    def test_compile_error_without_xml_fails(self):
        with patch.object(self.runner, "command", return_value=[sys.executable, "-c", "import sys; print('compile error'); sys.exit(1)"]):
            result = self.runner.run()
        self.assertFalse(result["passed"])
        self.assertIn("compile error", result["output_tail"])

    def test_sources_changed_during_test_fail_verification(self):
        command = self.report_command()
        command[-1] = command[-1].replace("sys.exit(0)", "Path('Example.java').write_text('changed by build'); sys.exit(0)")
        with patch.object(self.runner, "command", return_value=command):
            self.assertFalse(self.runner.run()["passed"])

    @unittest.skipUnless(os.environ.get("RUN_JAVA_INTEGRATION") == "1", "Set RUN_JAVA_INTEGRATION=1 with Maven/JDK on PATH")
    def test_real_maven_failure_then_repair(self):
        from demo_phase3 import run_demo
        agent = run_demo(self.root / "real-java")
        self.assertEqual(agent.state.status, "completed", agent.state.error)
        self.assertEqual(len(agent.state.test_results), 2)
        self.assertEqual(agent.state.test_results[0]["failures"], 3)
        self.assertTrue(agent.state.test_results[1]["passed"])
        self.assertEqual(agent.state.test_results[1]["tests"], 4)
        self.assertIn("4 total, 4 passed", agent.state.pr_description)
        self.assertIn("| 1 | 4 | 3 |", agent.state.pr_description)
        self.assertEqual(len(agent.state.changed_files), 2)


if __name__ == "__main__":
    unittest.main()
