"""Application adapters. Artifacts and original source snapshot remain on disk."""
from dataclasses import asdict
import base64
import json
from . import PersistenceError
from .runtime import RuntimePersistence, validate_resume
from ..core.state import AgentState


def settings_for(agent, test_timeout):
    def model(client):
        return {"provider": getattr(client, "provider", "anthropic"),
                "model": getattr(client, "model", "unknown")}
    return {"execution": model(agent.model), "verification": model(agent.verification_model),
            "budget": asdict(agent.budget.limits),
            "recovery_policy": asdict(agent.recovery_policy),
            "max_steps": agent.max_steps, "test_timeout": test_timeout,
            "state_schema_version": 1,
            "baseline_fingerprint": agent.changes.fingerprint()}


def attach(store, agent, test_timeout):
    settings = settings_for(agent, test_timeout)
    run_id = store.create_run(agent, settings)
    path = agent.tracer.directory / "runtime-baseline.json"
    path.write_text(json.dumps({name: base64.b64encode(data).decode() for name, data in
                               agent.changes.baseline.items()}), encoding="utf-8")
    agent.persistence = RuntimePersistence(store, run_id, settings)
    return run_id


def restore(store, run_id, model_factory, agent_factory):
    from ..workspace.workspace import Workspace
    from ..trace.tracer import Tracer
    run, checkpoint = store.load(run_id)
    settings = run["settings"]
    if settings.get("state_schema_version") != 1 or checkpoint["phase"] not in {
            "execution", "verification", "delivery", "artifacts", "terminal"}:
        raise PersistenceError("Unsupported persisted runtime schema/phase; resume refused")
    workspace = Workspace(run["repo_path"])
    # Validate before constructing clients or creating any artifacts.
    from ..workspace.changes import RepositoryChanges
    from types import SimpleNamespace
    validate_resume(SimpleNamespace(workspace=workspace, changes=RepositoryChanges(workspace)), run, checkpoint)
    relative = run["artifact_directory"]
    if relative != f".task_outputs/{run_id}":
        raise PersistenceError("Invalid artifact directory in persisted Run")
    directory = workspace.resolve(relative, internal=True)
    try:
        baseline = {name: base64.b64decode(data, validate=True) for name, data in
                    json.loads((directory / "runtime-baseline.json").read_text(encoding="utf-8")).items()}
    except Exception:
        raise PersistenceError("Original runtime baseline artifact missing or invalid; resume refused") from None
    import hashlib
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in baseline.items()}
    fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if fingerprint != settings["baseline_fingerprint"]:
        raise PersistenceError("Runtime baseline artifact was modified; resume refused")
    model = model_factory(settings["execution"])
    reviewer = model_factory(settings["verification"])
    agent = agent_factory(workspace, checkpoint["state"]["task"], model, settings["max_steps"],
                          verification_model=reviewer, test_timeout=settings["test_timeout"],
                          tracer=Tracer(workspace, relative=relative))
    # Existing tools hold references to state; restore fields in place.
    restored = AgentState(**checkpoint["state"])
    agent.state.__dict__.update(restored.__dict__)
    agent.changes.baseline = baseline
    from ..core.recovery import BudgetLimits, RecoveryPolicy
    agent.recovery_policy = RecoveryPolicy(**settings.get("recovery_policy", {}))
    agent.budget.limits = BudgetLimits(**settings.get("budget", {}))
    agent.safe_state = agent.state.to_dict()
    agent.safe_phase = checkpoint["phase"]
    agent.resume_phase = checkpoint["phase"]
    agent.persistence = RuntimePersistence(store, run_id, settings)
    return agent
