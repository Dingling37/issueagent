import hashlib
import subprocess
from . import PersistenceError


def git(workspace, *args):
    result = subprocess.run(["git", *args], cwd=workspace.root, capture_output=True, timeout=30)
    if result.returncode:
        raise PersistenceError("Cannot inspect Git state for persistence/resume")
    return result.stdout


def identity(agent):
    # Index contents, tracked/untracked bytes (including excluded source paths), and file modes.
    workspace = agent.workspace
    digest = hashlib.sha256()
    for args in (("status", "--porcelain=v1", "-z", "--untracked-files=all"),
                 ("diff", "--binary", "--no-ext-diff", "--no-textconv"),
                 ("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv"),
                 ("ls-files", "--stage", "-z")):
        digest.update(git(workspace, *args, "--", ".", ":(exclude).task_outputs"))
    names = set(git(workspace, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
    for raw in sorted(names - {b""}):
        name = raw.decode("utf-8")
        if name.startswith(".task_outputs/"):
            continue
        path = workspace.resolve(name, internal=True)
        digest.update(raw + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"<deleted>")
    return {"git_head": git(workspace, "rev-parse", "HEAD").decode().strip(),
            "source_fingerprint": agent.changes.fingerprint(),
            "working_tree_fingerprint": digest.hexdigest()}


class RuntimePersistence:
    def __init__(self, store, run_id, settings):
        self.store, self.run_id, self.settings = store, run_id, settings

    def checkpoint(self, agent, phase):
        self.store.checkpoint(self.run_id, agent.state, phase, identity(agent),
                              self.settings["execution"]["provider"], self.settings["execution"]["model"])

    def pause(self, agent, phase):
        # Reuse the last durable identity: a partial edit must not bless new bytes.
        _, checkpoint = self.store.load(self.run_id)
        from ..core.state import AgentState
        from copy import deepcopy
        # The durable snapshot also wins if interruption happened after a DB commit
        # but before Runtime refreshed its in-memory safe state.
        counters = {key: deepcopy(getattr(agent.state, key)) for key in (
            "budget_usage", "tool_calls", "test_runs", "runtime_seconds", "token_usage")}
        status, error = agent.state.status, agent.state.error
        agent.state.__dict__.update(AgentState(**checkpoint["state"]).__dict__)
        if checkpoint["phase"] == "terminal":
            return  # A committed terminal result is authoritative.
        agent.state.__dict__.update(counters)
        agent.state.status, agent.state.error = status, error
        self.store.checkpoint(self.run_id, agent.state, checkpoint["phase"],
            {key: checkpoint[key] for key in (
                "git_head", "source_fingerprint", "working_tree_fingerprint")},
            self.settings["execution"]["provider"], self.settings["execution"]["model"])

    def model_call(self, agent, purpose, duration, response=None, *, status=None):
        from ..trace.usage import normalized_tokens
        config = self.settings[purpose]
        self.store.model_call(self.run_id, step=agent.state.step_count, purpose=purpose,
            provider=config["provider"], model=(response.model_id or config["model"]) if response else config["model"],
            latency=duration, status=status or (
                "truncated" if response and response.stop_reason == "max_tokens" else
                "response" if response else "error"),
            raw_usage=response.raw_usage if response else None,
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
            **normalized_tokens(response.raw_usage if response else None))


def validate_resume(agent, run, checkpoint):
    if run["status"].upper() in {"COMPLETED", "FAILED", "CANCELLED"} or checkpoint["phase"] == "terminal" or checkpoint["state"]["status"] == "completed":
        raise PersistenceError("Run is terminal; cannot resume")
    from ..core.recovery import StateConflict
    branch = git(agent.workspace, "branch", "--show-current").decode().strip()
    if branch != run["task_branch"]:
        raise StateConflict("Resume refused: task branch changed")
    current = identity(agent)
    if any(current[key] != checkpoint[key] for key in current):
        raise StateConflict("Resume refused: Git HEAD, working tree/index or source fingerprint changed")
