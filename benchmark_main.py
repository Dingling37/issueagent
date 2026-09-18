import argparse
import json
from pathlib import Path

from issue_agent.benchmark.runner import BenchmarkCase, BenchmarkRunner
from issue_agent.core.agent import Agent
from issue_agent.core.model import AnthropicModelClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one isolated Issue Agent benchmark")
    parser.add_argument("--source-repo", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--issue-file", required=True, type=Path)
    parser.add_argument("--hidden-tests", required=True, type=Path)
    parser.add_argument("--hidden-destination", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case-id", default="java-001")
    parser.add_argument("--model")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--test-timeout", type=int, default=600)
    args = parser.parse_args()
    issue = args.issue_file.read_text(encoding="utf-8-sig")
    model = AnthropicModelClient(args.model)
    factory = lambda workspace, task: Agent(workspace, task, model, args.max_steps,
                                             test_timeout=args.test_timeout,
                                             verification_model=model)
    case = BenchmarkCase(args.case_id, args.source_repo, args.commit, issue,
                         args.hidden_tests, args.hidden_destination)
    result = BenchmarkRunner(args.output, factory, test_timeout=args.test_timeout).run(case)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["agent_completed"] and result["hidden_tests_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
