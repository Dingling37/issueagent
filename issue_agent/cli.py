"""Small interactive shell around the shared application entry point."""
import argparse
import getpass
import subprocess
import sys
import warnings
from pathlib import Path

from .application import run_issue, resume_issue
from .config import ConfigStore, ModelConfig, PROVIDERS
from .core.model import create_model_client
from .security import protect_secret, redact
from .terminal import create_terminal_observer
from .workspace.repository import inspect_repository
from .workspace.workspace import Workspace


def current_repository():
    workspace = Workspace(Path.cwd())
    inspect_repository(workspace)
    for args in (("status", "--porcelain=v1", "--untracked-files=all"),
                 ("rev-parse", "--verify", "HEAD^{commit}")):
        result = subprocess.run(["git", *args], cwd=workspace.root, capture_output=True,
                                text=True, encoding="utf-8", timeout=15)
        if result.returncode or (args[0] == "status" and result.stdout.strip()):
            raise ValueError("Start issue-agent in a clean Git repository root with an existing commit")
    return workspace.root


def configure(store, current=None):
    print("Configure model (providers: " + ", ".join(PROVIDERS) + ")")
    provider = input(f"Provider [{current.provider if current else 'anthropic'}]: ").strip()
    provider = provider or (current.provider if current else "anthropic")
    default_model = current.model if current and provider == current.provider else ""
    model = input(f"Model [{default_model}]: ").strip() or default_model
    ModelConfig(provider, model)  # Validate before asking for a secret.
    # getpass must fail closed when echo-free terminal input is unavailable.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        key = getpass.getpass("API Key (hidden; blank keeps stored key): ").strip()
    protect_secret(key)
    config = ModelConfig(provider, model, key)
    store.save(config)
    print("Configuration saved. API Key is stored only in system credentials.")
    return ModelConfig(provider, model)


def read_issue(text=None, file=None):
    if file is not None:
        path = Path(file)
        if path.suffix.lower() not in {".md", ".txt"}:
            raise ValueError("Issue file must be .md or .txt")
        text = path.read_text(encoding="utf-8-sig")
    elif text is None:
        mode = input("Issue source: [1] multiline text [2] .md/.txt file (default 1): ").strip()
        if mode == "2":
            return read_issue(file=input("Issue file: ").strip())
        if mode not in {"", "1"}:
            raise ValueError("Choose 1 or 2")
        print("Enter Issue; finish with a line containing only .end")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == ".end":
                break
            lines.append(line)
        text = "\n".join(lines)
    if not text.strip() or len(text) > 20000:
        raise ValueError("Issue must contain 1–20000 characters")
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run Issue Agent in the current clean Git repository")
    parser.add_argument("command", nargs="?", choices=["config", "resume"])
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--edit", action="store_true", help="Edit saved configuration (with config)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--issue")
    source.add_argument("--issue-file", type=Path)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--test-timeout", type=int, default=300)
    for name in ("llm-calls", "tool-calls", "test-runs", "total-tokens", "runtime-seconds"):
        parser.add_argument("--budget-" + name, type=float if name == "runtime-seconds" else int)
    args = parser.parse_args(argv)
    from .core.recovery import BudgetLimits
    values = {name: getattr(args, "budget_" + name) for name in (
        "llm_calls", "tool_calls", "test_runs", "total_tokens", "runtime_seconds")}
    try:
        budget_options = {"budget": BudgetLimits(**values)} if any(v is not None for v in values.values()) else {}
        if args.edit and args.command != "config":
            raise ValueError("--edit requires config")
        if args.command == "resume":
            if not args.run_id or args.issue or args.issue_file:
                raise ValueError("Use issue-agent resume <run_id> without a new Issue")
            store = ConfigStore()
            return resume_issue(args.run_id, model_factory=lambda saved: create_model_client(
                store.resolve(ModelConfig(saved["provider"], saved["model"]))),
                observer_factory=create_terminal_observer, **budget_options)
        if args.run_id:
            raise ValueError("Run ID is only valid with resume")
        store = ConfigStore()
        if args.command == "config":
            try:
                config = store.load()
            except ValueError:
                if not args.edit:
                    raise
                config = None
            if config is None or args.edit:
                config = configure(store, config)
            print(f"Provider: {config.provider}\nModel: {config.model}")
            print("API Key: " + ("stored in system credentials" if store.credentials.get(config.provider) else "missing"))
            return 0
        repo = current_repository()
        if not 1 <= args.max_steps <= 200 or not 1 <= args.test_timeout <= 1800:
            raise ValueError("Invalid max steps or test timeout")
        config = store.load() or configure(store)
        resolved = store.resolve(config)
        issue = read_issue(args.issue, args.issue_file)
        return run_issue(repo, issue, max_steps=args.max_steps, test_timeout=args.test_timeout,
                         model_factory=lambda _: create_model_client(resolved),
                         observer_factory=create_terminal_observer, **budget_options)
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Never print third-party exception bodies: credential backends may echo secrets.
        print(redact(f"Issue Agent: {exc}") if isinstance(exc, (ValueError, FileNotFoundError))
              else "Issue Agent: startup failed; check configuration and system credential store", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
