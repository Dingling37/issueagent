import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from issue_agent.core.agent import Agent
from issue_agent.core.model import ModelResponse, ScriptedModelClient
from issue_agent.persistence import PersistenceError
from issue_agent.persistence.schema import metadata, checkpoints, model_calls
from issue_agent.persistence.store import Store
from issue_agent.persistence.launch import attach, restore
from issue_agent.trace.usage import normalized_tokens
from issue_agent.verification.verifier import VerificationResult
from issue_agent.workspace.repository import prepare_task_branch
from issue_agent.workspace.workspace import Workspace


class PassingVerifier:
    def verify(self, state):
        return VerificationResult(True, "passed")


class InterruptModel:
    model = "test-model"
    provider = "deepseek"
    def call(self, *args, **kwargs):
        raise KeyboardInterrupt()


class PersistenceTests(unittest.TestCase):
    def make_engine(self):
        return create_engine("sqlite://")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = self.make_engine()
        metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.store = Store(self.engine)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / ".gitignore").write_text(".task_outputs/\n")
        (self.root / "source.txt").write_text("original")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.workspace = Workspace(self.root)
        self.branch = prepare_task_branch(self.workspace)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.DEVNULL)

    def agent(self, model):
        agent = Agent(self.workspace, "fix issue", model, verifier=PassingVerifier())
        agent.state.git_task = self.branch
        attach(self.store, agent, 300)
        return agent

    def resumed(self, run_id, model):
        return restore(self.store, run_id, lambda _: model,
                       lambda *a, **kw: Agent(*a, **kw, verifier=PassingVerifier()))

    def test_resume_restores_canonical_state_baseline_and_artifacts(self):
        read = ModelResponse([{"type": "tool_use", "id": "read", "name": "read_file",
            "input": {"path": "source.txt", "start_line": 1, "end_line": 10}}])
        class ReadThenInterrupt(ScriptedModelClient):
            def call(self, *args, **kwargs):
                if not self.responses:
                    raise KeyboardInterrupt()
                return super().call(*args, **kwargs)
        agent = self.agent(ReadThenInterrupt([read]))
        agent.run()
        run_id = agent.persistence.run_id
        run, cp = self.store.load(run_id)
        self.assertEqual(cp["state"]["step_count"], 1)
        self.assertEqual(cp["state"]["messages"][-1]["content"][0]["tool_use_id"], "read")
        resumed = self.resumed(run_id, ScriptedModelClient([ModelResponse([{"type": "text", "text": "done"}])]))
        self.assertEqual(resumed.changes.baseline, agent.changes.baseline)
        self.assertEqual(resumed.tracer.relative, agent.tracer.relative)
        self.assertEqual(resumed.run().status, "completed")
        self.assertEqual(resumed.state.step_count, 2)
        self.assertEqual(len(resumed.state.tool_history), 1)
        self.assertEqual(resumed.state.llm_calls, 2)
        with self.assertRaises(PersistenceError):
            self.resumed(run_id, InterruptModel())

    def test_resume_refuses_external_changes(self):
        for kind in ("source", "untracked", "index", "head", "branch"):
            with self.subTest(kind=kind):
                agent = self.agent(InterruptModel())
                agent.run()
                if kind == "source":
                    (self.root / "source.txt").write_text("outside")
                elif kind == "untracked":
                    (self.root / "new.txt").write_text("outside")
                elif kind == "index":
                    self.git("update-index", "--chmod=+x", "source.txt")
                elif kind == "head":
                    self.git("commit", "--allow-empty", "-qm", "outside")
                else:
                    self.git("checkout", "-qb", "outside")
                with self.assertRaises(PersistenceError):
                    self.resumed(agent.persistence.run_id, InterruptModel())
                self.git("checkout", self.branch["task_branch"])
                self.git("reset", "--hard", self.branch["base_commit"])
                if (self.root / "new.txt").exists():
                    (self.root / "new.txt").unlink()

    def test_resume_verification_does_not_repeat_execution_model(self):
        agent = self.agent(InterruptModel())
        agent.state.plan = agent.planner.create(agent.state, {})
        agent.state.step_count = 20
        agent.state.final_response = "done"
        agent.checkpoint("verification")
        resumed = self.resumed(agent.persistence.run_id, InterruptModel())
        self.assertEqual(resumed.run().status, "completed")
        self.assertEqual(resumed.state.llm_calls, 0)

    def test_resume_delivery_does_not_repeat_verifier(self):
        agent = self.agent(InterruptModel())
        agent.state.verification_results = [{"passed": True, "feedback": "passed", "infrastructure_error": False}]
        agent.checkpoint("delivery")
        resumed = self.resumed(agent.persistence.run_id, InterruptModel())
        resumed.verifier.verify = lambda _: self.fail("Verifier repeated")
        self.assertEqual(resumed.run().status, "completed")

    def test_database_failure_propagates_without_later_model_calls(self):
        agent = self.agent(InterruptModel())
        with patch.object(self.store, "checkpoint", side_effect=PersistenceError("database unavailable")):
            with self.assertRaisesRegex(PersistenceError, "unavailable"):
                agent.run()
        self.assertEqual(agent.state.llm_calls, 0)

    def test_tokens_and_usage_missing_values(self):
        self.assertEqual(normalized_tokens(None), {"cached_input": None, "uncached_input": None})
        self.assertEqual(normalized_tokens({"input_tokens": 10, "cache_read_input_tokens": 50}),
                         {"cached_input": 50, "uncached_input": 10})
        agent = self.agent(InterruptModel())
        response = ModelResponse([], 15, 3, raw_usage={"prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 5})
        agent.persistence.model_call(agent, "verification", 0.5, response)
        with self.engine.connect() as connection:
            row = connection.execute(select(model_calls)).mappings().one()
        self.assertEqual((row["cached_input"], row["uncached_input"], row["output_tokens"]), (10, 5, 3))
        self.assertEqual(row["purpose"], "verification")

    def test_schema_uses_postgresql_jsonb(self):
        self.assertEqual(str(checkpoints.c.state.type.compile(dialect=postgresql.dialect())), "JSONB")

    def test_invalid_database_url_is_explicit(self):
        with patch.dict(os.environ, {"ISSUE_AGENT_DATABASE_URL": "sqlite://"}):
            with self.assertRaises(PersistenceError):
                Store.from_environment()

    def test_baseline_tamper_refused(self):
        agent = self.agent(InterruptModel())
        agent.run()
        (agent.tracer.directory / "runtime-baseline.json").write_text("{}")
        with self.assertRaises(PersistenceError):
            self.resumed(agent.persistence.run_id, InterruptModel())

    def test_partial_tool_group_resumes_after_confirmed_mutation(self):
        response = ModelResponse([
            {"type": "tool_use", "id": "first", "name": "create_file", "input": {"path": "new.txt", "content": "new"}},
            {"type": "tool_use", "id": "second", "name": "git_status", "input": {}}])
        agent = self.agent(ScriptedModelClient([response]))
        from dataclasses import replace
        agent.registry.tools["git_status"] = replace(agent.registry.tools["git_status"],
            handler=lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        agent.run()
        _, cp = self.store.load(agent.persistence.run_id)
        self.assertEqual(len(cp["state"]["tool_history"]), 1)
        self.assertEqual(cp["state"]["pending_tool_batch"]["results"][0]["tool_use_id"], "first")
        resumed = self.resumed(agent.persistence.run_id, ScriptedModelClient([ModelResponse([{
            "type": "text", "text": "done"}])]))
        self.assertEqual(resumed.run().status, "completed")
        self.assertEqual((self.root / "new.txt").read_text(), "new")
        self.assertEqual([item["name"] for item in resumed.state.tool_history], ["create_file", "git_status"])

    def test_resume_committed_artifacts_keeps_git_delivery(self):
        from issue_agent.delivery.commit import commit_verified_task
        agent = Agent(self.workspace, "fix issue", InterruptModel())
        agent.state.git_task = self.branch
        attach(self.store, agent, 300)
        (self.root / "source.txt").write_text("fixed")
        agent.state.files_modified = ["source.txt"]
        agent.state.verification_results = [{"passed": True, "feedback": "passed", "infrastructure_error": False}]
        agent.state.test_results = [{"passed": True, "fingerprint": agent.changes.fingerprint(),
            "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "command": ["test"]}]
        agent.tracer.save("task.diff", agent.changes.diff()[1])
        commit_verified_task(agent.state, agent.changes)
        agent.checkpoint("artifacts")
        head = self.git("rev-parse", "HEAD")
        resumed = restore(self.store, agent.persistence.run_id, lambda _: InterruptModel(), Agent)
        with patch("issue_agent.core.agent.commit_verified_task", side_effect=AssertionError("repeat commit")):
            self.assertEqual(resumed.run().status, "completed")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(resumed.state.delivery_diff["source"], "git_commit_range")
        self.assertTrue((resumed.tracer.directory / "pr-description.md").is_file())

    def test_complete_verifier_test_has_a_safe_checkpoint(self):
        agent = self.agent(InterruptModel())
        agent.in_verification = True
        agent.state.final_response = "done"
        # No build file: the completed failed test is still a safe, structured result.
        result = agent.runner.run()
        _, cp = self.store.load(agent.persistence.run_id)
        self.assertEqual(cp["phase"], "verification")
        self.assertEqual(cp["state"]["test_results"], [result])

    def test_database_error_during_verifier_is_not_converted_to_model_feedback(self):
        agent = Agent(self.workspace, "fix issue", InterruptModel())
        agent.state.git_task = self.branch
        attach(self.store, agent, 300)
        (self.root / "source.txt").write_text("fixed")
        agent.state.files_modified = ["source.txt"]
        agent.state.test_results = [{"passed": True, "fingerprint": agent.changes.fingerprint()}]
        agent.verifier.review = lambda *args: (_ for _ in ()).throw(PersistenceError("database unavailable"))
        with self.assertRaises(PersistenceError):
            agent.verifier.verify(agent.state)

    def test_database_preflight_precedes_branch_creation(self):
        from issue_agent.application import run_issue
        with patch.object(Store, "from_environment", return_value=self.store), \
             patch.object(self.store, "check", side_effect=PersistenceError("database unavailable")), \
             patch("issue_agent.application.prepare_task_branch") as branch:
            with self.assertRaises(PersistenceError):
                run_issue(self.root, "task", model_factory=lambda _: InterruptModel())
            branch.assert_not_called()

    def test_cli_resume_does_not_require_clean_task_worktree(self):
        from issue_agent import cli
        with patch.object(cli, "resume_issue", return_value=0) as resume, \
             patch.object(cli, "current_repository", side_effect=AssertionError("clean-start check")):
            self.assertEqual(cli.main(["resume", "saved-run"]), 0)
            self.assertEqual(resume.call_args.args, ("saved-run",))


@unittest.skipUnless(os.environ.get("ISSUE_AGENT_TEST_DATABASE_URL"), "PostgreSQL test database not configured")
class PostgreSQLPersistenceTests(PersistenceTests):
    """Run the same resume integration scenarios against a freshly migrated PG schema."""
    def make_engine(self):
        from uuid import uuid4
        from sqlalchemy.schema import CreateSchema, DropSchema
        from sqlalchemy.engine import make_url
        from alembic.config import Config
        from alembic import command
        schema = "issue_agent_test_" + uuid4().hex
        url = make_url(os.environ["ISSUE_AGENT_TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        def cleanup():
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
            admin.dispose()
        self.addCleanup(cleanup)
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        return engine

    def test_concurrent_resume_lock_refused(self):
        agent = self.agent(InterruptModel())
        with self.store.lease(agent.persistence.run_id):
            with self.assertRaisesRegex(PersistenceError, "already active"):
                with self.store.lease(agent.persistence.run_id):
                    self.fail("Concurrent lease acquired")


if __name__ == "__main__":
    unittest.main()
