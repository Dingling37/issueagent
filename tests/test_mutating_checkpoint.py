import os
import unittest
from dataclasses import replace
from unittest.mock import patch
from sqlalchemy import select
import test_persistence as persistence
from issue_agent.core.model import ModelResponse, ScriptedModelClient
from issue_agent.persistence import PersistenceError
from issue_agent.persistence.runtime import identity
from issue_agent.persistence.schema import checkpoints
from issue_agent.tools.base import Tool

DONE = ModelResponse([{"type": "text", "text": "done"}])

def call(name, arguments, identifier):
    return {"type": "tool_use", "id": identifier, "name": name, "input": arguments}

def edit(old, new, identifier):
    return call("edit_file", {"path": "source.txt", "old_text": old, "new_text": new}, identifier)

class MutatingCheckpointTests(unittest.TestCase):
    setUp = persistence.PersistenceTests.setUp
    make_engine = persistence.PersistenceTests.make_engine
    git = persistence.PersistenceTests.git
    agent = persistence.PersistenceTests.agent
    resumed = persistence.PersistenceTests.resumed

    def snapshots(self, agent):
        with self.engine.connect() as conn:
            return list(conn.execute(select(checkpoints).where(
                checkpoints.c.run_id == agent.persistence.run_id).order_by(checkpoints.c.sequence)).mappings())

    def interrupt_tool(self, agent, name):
        agent.registry.tools[name] = replace(agent.registry.tools[name],
            handler=lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt()))

    def test_two_edits_checkpoint_first_before_second(self):
        agent = self.agent(ScriptedModelClient([ModelResponse([
            edit("original", "first", "1"), edit("first", "second", "2")]), DONE]))
        original = agent.registry.tools["edit_file"].handler
        def inspect_then_edit(**kwargs):
            if kwargs["old_text"] == "first":
                _, cp = self.store.load(agent.persistence.run_id)
                self.assertEqual(cp["state"]["pending_tool_batch"]["results"][0]["tool_use_id"], "1")
                self.assertEqual(len(cp["state"]["tool_history"]), 1)
                self.assertEqual((self.root / "source.txt").read_text(), "first")
                for key, value in identity(agent).items():
                    self.assertEqual(cp[key], value)
            return original(**kwargs)
        agent.registry.tools["edit_file"] = replace(agent.registry.tools["edit_file"], handler=inspect_then_edit)
        self.assertEqual(agent.run().status, "completed")
        execution = [cp for cp in self.snapshots(agent) if cp["phase"] == "execution"]
        self.assertEqual(len(execution), 4)  # initial, edit 1, edit 2, batch
        self.assertEqual([len(cp["state"]["pending_tool_batch"].get("results", [])) for cp in execution], [0, 1, 2, 0])

    def test_edit_interrupt_and_resume_skips_completed_edit(self):
        model = ScriptedModelClient([ModelResponse([edit("original", "first", "1"),
            call("git_status", {}, "2"), edit("first", "second", "3")])])
        agent = self.agent(model)
        self.interrupt_tool(agent, "git_status")
        self.assertEqual(agent.run().status, "paused_user")
        _, cp = self.store.load(agent.persistence.run_id)
        self.assertEqual(len(cp["state"]["pending_tool_batch"]["results"]), 1)
        resumed_model = ScriptedModelClient([DONE])
        resumed = self.resumed(agent.persistence.run_id, resumed_model)
        self.assertEqual(resumed.run().status, "completed")
        self.assertEqual((self.root / "source.txt").read_text(), "second")
        self.assertEqual([h["name"] for h in resumed.state.tool_history], ["edit_file", "git_status", "edit_file"])
        self.assertEqual([r["tool_use_id"] for r in resumed_model.contexts[0][-2]["content"]], ["1", "2", "3"])
        self.assertTrue(all(not r["is_error"] for r in resumed_model.contexts[0][-2]["content"]))
        self.assertEqual(resumed.state.step_count, 2)
        self.assertEqual(len(resumed_model.contexts), 1)

    def test_hard_process_stop_uses_mutating_checkpoint_without_pause(self):
        agent = self.agent(ScriptedModelClient([ModelResponse([edit("original", "first", "1"), call("git_status", {}, "2")])]))
        agent.registry.tools["git_status"] = replace(agent.registry.tools["git_status"],
            handler=lambda: (_ for _ in ()).throw(SystemExit(17)))
        with self.assertRaises(SystemExit):
            agent.run()
        run, cp = self.store.load(agent.persistence.run_id)
        self.assertEqual(run["status"], "RUNNING")
        self.assertEqual(cp["state"]["pending_tool_batch"]["results"][0]["tool_use_id"], "1")
        resumed = self.resumed(agent.persistence.run_id, ScriptedModelClient([DONE]))
        self.assertEqual(resumed.run().status, "completed")
        self.assertEqual(len(resumed.state.tool_history), 2)

    def test_abrupt_process_exit_after_confirmed_write(self):
        import subprocess
        import sys
        from sqlalchemy import create_engine, text
        from issue_agent.persistence.schema import metadata
        from issue_agent.persistence.store import Store
        if self.engine.dialect.name == "sqlite":
            directory = self.root / ".task_outputs"
            directory.mkdir(exist_ok=True)
            self.engine = create_engine("sqlite:///" + str(directory / "test-runtime.db"))
            self.addCleanup(self.engine.dispose)
            metadata.create_all(self.engine)
            self.store = Store(self.engine)
        agent = self.agent(persistence.InterruptModel())
        agent.state.plan = agent.planner.create(agent.state, {})
        agent.checkpoint("execution")
        with self.engine.connect() as connection:
            schema = connection.scalar(text("select current_schema()")) if self.engine.dialect.name == "postgresql" else ""
        script = r"""
import os, sys
from dataclasses import replace
from sqlalchemy import create_engine
from issue_agent.core.agent import Agent
from issue_agent.core.model import ModelResponse, ScriptedModelClient
from issue_agent.persistence.store import Store
from issue_agent.persistence.launch import restore
from issue_agent.verification.verifier import VerificationResult
class PassingVerifier:
    def verify(self, state): return VerificationResult(True, 'passed')
options = {'options': '-csearch_path=' + sys.argv[2]} if sys.argv[2] else {}
engine = create_engine(os.environ['MUTATING_CHECKPOINT_TEST_URL'], connect_args=options)
model = ScriptedModelClient([ModelResponse([
    {'type': 'tool_use', 'id': '1', 'name': 'edit_file', 'input': {'path': 'source.txt', 'old_text': 'original', 'new_text': 'first'}},
    {'type': 'tool_use', 'id': '2', 'name': 'git_status', 'input': {}}])])
agent = restore(Store(engine), sys.argv[1], lambda _: model,
    lambda *a, **kw: Agent(*a, **kw, verifier=PassingVerifier()))
agent.registry.tools['git_status'] = replace(agent.registry.tools['git_status'], handler=lambda: os._exit(17))
agent.run()
"""
        environment = dict(os.environ, MUTATING_CHECKPOINT_TEST_URL=self.engine.url.render_as_string(hide_password=False))
        process = subprocess.run([sys.executable, "-c", script, agent.persistence.run_id, schema],
                                 env=environment, capture_output=True, timeout=60)
        self.assertEqual(process.returncode, 17, process.stderr.decode(errors="replace"))
        run, cp = self.store.load(agent.persistence.run_id)
        self.assertEqual(run["status"], "RUNNING")
        self.assertEqual(len(cp["state"]["pending_tool_batch"]["results"]), 1)
        resumed = self.resumed(agent.persistence.run_id, ScriptedModelClient([DONE]))
        self.assertEqual(resumed.run().status, "completed")
        self.assertEqual((self.root / "source.txt").read_text(), "first")
        self.assertEqual(len(resumed.state.tool_history), 2)

    def test_interrupt_after_checkpoint_commit_before_memory_refresh(self):
        agent = self.agent(ScriptedModelClient([ModelResponse([edit("original", "first", "1")])]))
        original = self.store.checkpoint
        interrupted = False
        def commit_then_interrupt(run_id, state, *args):
            nonlocal interrupted
            original(run_id, state, *args)
            if state.pending_tool_batch.get("results") and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
        with patch.object(self.store, "checkpoint", side_effect=commit_then_interrupt):
            self.assertEqual(agent.run().status, "paused_user")
        _, cp = self.store.load(agent.persistence.run_id)
        self.assertEqual(len(cp["state"]["pending_tool_batch"]["results"]), 1)
        resumed = self.resumed(agent.persistence.run_id, ScriptedModelClient([DONE]))
        self.assertEqual(resumed.run().status, "completed")
        self.assertEqual(len(resumed.state.tool_history), 1)
        self.assertEqual((self.root / "source.txt").read_text(), "first")

    def test_unconfirmed_write_window_still_refuses_resume(self):
        agent = self.agent(ScriptedModelClient([ModelResponse([edit("original", "first", "1")])]))
        handler = agent.registry.tools["edit_file"].handler
        def write_then_interrupt(**kwargs):
            handler(**kwargs)
            raise KeyboardInterrupt()
        agent.registry.tools["edit_file"] = replace(agent.registry.tools["edit_file"], handler=write_then_interrupt)
        self.assertEqual(agent.run().status, "paused_user")
        self.assertEqual((self.root / "source.txt").read_text(), "first")
        with self.assertRaisesRegex(PersistenceError, "changed"):
            self.resumed(agent.persistence.run_id, ScriptedModelClient([DONE]))

    def test_create_interrupt_resume(self):
        agent = self.agent(ScriptedModelClient([ModelResponse([call("create_file", {"path": "new.txt", "content": "new"}, "1"),
            call("git_status", {}, "2")])]))
        self.interrupt_tool(agent, "git_status")
        self.assertEqual(agent.run().status, "paused_user")
        resumed = self.resumed(agent.persistence.run_id, ScriptedModelClient([DONE]))
        self.assertEqual(resumed.run().status, "completed")
        self.assertEqual((self.root / "new.txt").read_text(), "new")
        self.assertEqual(len([h for h in resumed.state.tool_history if h["name"] == "create_file"]), 1)

    def test_read_tools_have_only_existing_batch_checkpoint(self):
        agent = self.agent(ScriptedModelClient([ModelResponse([
            call("read_file", {"path": "source.txt", "start_line": 1, "end_line": 10}, "1"),
            call("search_code", {"query": "original"}, "2"), call("list_files", {}, "3")]), DONE]))
        self.assertEqual(agent.run().status, "completed")
        self.assertEqual([cp["phase"] for cp in self.snapshots(agent)],
            ["execution", "execution", "verification", "delivery", "terminal"])

    def test_failed_edit_and_create_have_no_success_checkpoint(self):
        agent = self.agent(ScriptedModelClient([ModelResponse([edit("missing", "bad", "1"),
            call("create_file", {"path": "source.txt", "content": "bad"}, "2")]), DONE]))
        self.assertEqual(agent.run().status, "completed")
        self.assertEqual((self.root / "source.txt").read_text(), "original")
        execution = [cp for cp in self.snapshots(agent) if cp["phase"] == "execution"]
        self.assertEqual(len(execution), 2)
        self.assertTrue(all(not h["ok"] for h in execution[-1]["state"]["tool_history"]))
        self.assertFalse(execution[-1]["state"]["pending_tool_batch"])

    def test_checkpoint_failure_stops_following_tool_and_resume_refuses(self):
        agent = self.agent(ScriptedModelClient([ModelResponse([edit("original", "first", "1"), edit("first", "second", "2")])]))
        original = self.store.checkpoint
        def fail_after_mutation(run_id, state, *args):
            if state.pending_tool_batch.get("results"):
                raise PersistenceError("database unavailable after edit")
            return original(run_id, state, *args)
        with patch.object(self.store, "checkpoint", side_effect=fail_after_mutation):
            with self.assertRaisesRegex(PersistenceError, "after edit"):
                agent.run()
        self.assertEqual((self.root / "source.txt").read_text(), "first")
        self.assertEqual(len(agent.state.tool_history), 1)
        self.assertEqual(len(self.snapshots(agent)), 1)
        with self.assertRaisesRegex(PersistenceError, "changed"):
            self.resumed(agent.persistence.run_id, ScriptedModelClient([DONE]))

    def test_metadata_drives_checkpoint_for_custom_tool(self):
        agent = self.agent(ScriptedModelClient([ModelResponse([call("custom_write", {}, "1"), call("git_status", {}, "2")])]))
        def write():
            (self.root / "source.txt").write_text("custom")
            return "written"
        agent.registry.register(Tool("custom_write", "custom", {}, write, mutates_workspace=True))
        self.interrupt_tool(agent, "git_status")
        self.assertEqual(agent.run().status, "paused_user")
        _, cp = self.store.load(agent.persistence.run_id)
        self.assertEqual(cp["state"]["pending_tool_batch"]["results"][0]["tool_use_id"], "1")
        self.assertEqual(cp["source_fingerprint"], agent.changes.fingerprint())
        self.assertEqual(self.resumed(agent.persistence.run_id, ScriptedModelClient([DONE])).run().status, "completed")

    def test_resume_pending_batch_at_max_steps(self):
        agent = self.agent(ScriptedModelClient([ModelResponse([edit("original", "first", "1"), call("git_status", {}, "2")])]))
        agent.max_steps = 1
        agent.persistence.settings["max_steps"] = 1
        self.store.update_settings(agent.persistence.run_id, agent.persistence.settings)
        self.interrupt_tool(agent, "git_status")
        agent.run()
        resumed = self.resumed(agent.persistence.run_id, persistence.InterruptModel())
        self.assertEqual(resumed.run().status, "failed")
        self.assertEqual(len(resumed.state.tool_history), 2)
        self.assertFalse(resumed.state.pending_tool_batch)
        self.assertEqual(resumed.state.step_count, 1)

    def test_metadata_does_not_change_model_schema(self):
        tool = Tool("write", "desc", {}, lambda: "ok")
        self.assertEqual(tool.schema(), replace(tool, mutates_workspace=True).schema())

@unittest.skipUnless(os.environ.get("ISSUE_AGENT_TEST_DATABASE_URL"), "PostgreSQL test database not configured")
class PostgreSQLMutatingCheckpointTests(MutatingCheckpointTests):
    make_engine = persistence.PostgreSQLPersistenceTests.make_engine
