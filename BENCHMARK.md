# Baseline Benchmark

## Frozen baseline

The pre-Benchmark Agent implementation is frozen in Git commit:

```text
66915c96cece2ee46acb18365153202cadfa3324
```

Benchmark code is an outer orchestration layer. It does not change Agent Loop, ContextManager,
Verifier prompts, tools, or the existing TestRunner behavior.

## Isolation sequence

1. Resolve the exact task repository commit.
2. Clone that commit into a new `agent-workspace`.
3. Run Agent to completion. Hidden test paths and contents are not placed in its workspace,
   state, context, verifier input, or trace.
4. After `Agent.run()` returns, copy its output into a separate `judge-workspace`.
5. Remove visible test sources from the Judge copy, then inject Hidden Tests from an external directory.
6. Run the existing Maven/Gradle `TestRunner`. This means its zero-test, all-skipped, timeout,
   XML freshness and failure rules remain in force.
7. Save `benchmark-result.json` outside both task workspaces.

Removing visible tests is important: otherwise visible tests could make the total non-zero even if
the hidden suite was not discovered. The Judge currently assumes conventional Java tests under
`src/test`; this is an MVP constraint.

This is application-level isolation. File tools are limited to Agent Workspace and Judge does not
exist during Agent execution. It is not an OS sandbox: a hostile repository build script executed by
Maven/Gradle may access other paths available to the current Windows account. Benchmark only trusted
repositories until process isolation is added.

## Result fields

- `agent_completed`: Agent state ended as `completed`.
- `verifier_passed`: Agent's final Verifier result passed.
- `hidden_tests_passed`: at least one non-skipped Hidden Test ran and the isolated TestRunner passed.
- `verifier_false_positive`: Verifier passed but Hidden Tests did not.
- `agent_metrics`: unchanged metrics emitted by the Agent baseline.
- `hidden_test_result`: structured TestRunner evidence from Judge.

## Commands

The deterministic smoke benchmark uses fixed model responses and does not consume API tokens:

```powershell
python demo_benchmark.py
```

For a real model, use `benchmark_main.py`; the task's Hidden Tests must stay outside `source-repo`:

```powershell
python benchmark_main.py `
  --source-repo C:\path\to\repo `
  --commit <fixed-commit> `
  --issue-file C:\path\to\issue.md `
  --hidden-tests C:\private\hidden-tests `
  --hidden-destination src/test/java/demo `
  --output C:\private\benchmark-runs
```

The CLI reads the same model environment variables as `main.py`. Run directories are unique, so
repeated runs start from the same commit without reusing prior modifications.
