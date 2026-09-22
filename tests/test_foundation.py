import json
import os
import tempfile
import unittest
from pathlib import Path

from issue_agent.core.agent import Agent
from issue_agent.core.model import ModelResponse, ScriptedModelClient
from issue_agent.core.state import AgentState
from issue_agent.context.manager import ContextManager
from issue_agent.tools.base import Tool, Permission
from issue_agent.verification.verifier import VerificationResult, PendingVerifier
from issue_agent.workspace.workspace import Workspace


def call(name: str, **arguments: object) -> ModelResponse:
    return ModelResponse([{"type": "tool_use", "id": "test-call", "name": name, "input": arguments}])


def done() -> ModelResponse:
    return ModelResponse([{"type": "text", "text": "Finished"}], 10, 2)


class FoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        (self.root / "Example.java").write_bytes(b"class Example {}\r\n")
        self.workspace = Workspace(self.root)

    def agent(self, responses: list[ModelResponse], **kwargs: object) -> Agent:
        kwargs.setdefault("verifier", PendingVerifier())
        return Agent(self.workspace, "Change Example and test it", ScriptedModelClient(responses), **kwargs)

    def test_workspace_escape_and_metadata(self) -> None:
        for path in ("../secret", "a/../../secret", "..\\secret", "/etc/passwd",
                     "C:\\secret", "file:stream", ".git/config", ".GIT/config", ".task_outputs/log"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.workspace.resolve(path)

    def test_hardlink_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("private")
        os.link(outside, self.root / "link.txt")
        with self.assertRaises(ValueError):
            self.workspace.resolve("link.txt")

    def test_symlink_rejected(self) -> None:
        try:
            (self.root / "link").symlink_to(Path(self.temp.name), target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit unprivileged symlinks")
        with self.assertRaises(ValueError):
            self.workspace.resolve("link/secret")
        self.assertNotIn("link", list(self.workspace.files()))

    def test_edit_error_feedback_and_repair(self) -> None:
        agent = self.agent([
            call("edit_file", path="Example.java", old_text="missing", new_text="bad"),
            call("read_file", path="Example.java", start_line=1, end_line=5),
            call("edit_file", path="Example.java", old_text="class Example {}", new_text="class Example { int value; }"),
            done(),
        ])
        state = agent.run()
        self.assertEqual(state.status, "verification_unavailable")
        self.assertIn("No match found after normalizing CRLF/LF", json.dumps(agent.model.contexts[1]))
        self.assertEqual(state.files_modified, ["Example.java"])
        self.assertEqual(state.files_read, ["Example.java"])
        self.assertTrue((self.root / "Example.java").read_bytes().endswith(b"\r\n"))
        self.assertEqual(state.token_usage, {"input": 10, "output": 2})
        events = [json.loads(line) for line in (agent.tracer.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("VERIFICATION_FAILED", [e["event_type"] for e in events])
        self.assertNotIn("TASK_COMPLETED", [e["event_type"] for e in events])
        self.assertTrue((agent.tracer.directory / "state.json").is_file())

    def test_step_limit(self) -> None:
        state = self.agent([call("list_files"), call("list_files")], max_steps=2).run()
        self.assertEqual(state.status, "failed")
        self.assertEqual(state.step_count, 2)

    def test_verifier_feedback_reenters_loop(self) -> None:
        class RejectThenAccept:
            count = 0

            def verify(self, state: AgentState) -> VerificationResult:
                self.count += 1
                return VerificationResult(self.count > 1, "Missing invalid-email test")

        agent = self.agent([done(), done()], verifier=RejectThenAccept())
        self.assertEqual(agent.run().status, "completed")
        self.assertIn("Missing invalid-email test", json.dumps(agent.model.contexts[1]))
        self.assertEqual(agent.state.retry_count, 1)

    def test_registry_validation_and_forbidden_tools(self) -> None:
        agent = self.agent([])
        for name, args in [("shell", {}), ("read_file", {"path": "Example.java"}),
                           ("list_files", {"extra": 1}),
                           ("read_file", {"path": "Example.java", "start_line": True, "end_line": 5}),
                           ("create_file", {"path": "../outside", "content": "bad"}),
                           ("create_file", {"path": "Example.java", "content": "bad"})]:
            with self.subTest(name=name, args=args):
                self.assertFalse(agent.registry.execute(name, args)[1])
        with self.assertRaises(ValueError):
            agent.registry.register(Tool("shell", "danger", {}, lambda: "", Permission.DANGEROUS))
        self.assertEqual((self.root / "Example.java").read_bytes(), b"class Example {}\r\n")

    def test_full_output_persisted_and_summary_bounded(self) -> None:
        agent = self.agent([])
        agent.registry.register(Tool("large", "test output", {}, lambda: "X" * 20000))
        output, ok = agent.registry.execute("large", {})
        self.assertTrue(ok)
        self.assertLess(len(output), 6500)
        path = self.root / json.loads(output)["full_output"]
        self.assertEqual(len(path.read_text()), 20000)

    def test_context_keeps_tool_pairs_and_state(self) -> None:
        state = AgentState("original issue", str(self.root), plan=[{"id": 1, "task": "test"}],
                           files_modified=["Example.java"])
        for index in range(30):
            state.messages.extend([
                {"role": "assistant", "content": [{"type": "tool_use", "id": str(index), "name": "list_files", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(index), "content": "X" * 6000}]},
            ])
        context = ContextManager().build(state)
        self.assertLess(len(json.dumps(context)), 50000)
        self.assertIn("original issue", context[0]["content"])
        self.assertIn("Example.java", context[0]["content"])
        self.assertEqual(len(state.messages), 60)
        for index in range(1, len(context), 2):
            self.assertEqual(context[index]["content"][0]["id"], context[index + 1]["content"][0]["tool_use_id"])

    def test_model_failure_persists_state(self) -> None:
        agent = self.agent([])
        self.assertEqual(agent.run().status, "failed")
        self.assertIn("exhausted", agent.state.error)
        self.assertTrue((agent.tracer.directory / "metrics.json").exists())


if __name__ == "__main__":
    unittest.main()
