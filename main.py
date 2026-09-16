# 阅读起点：解析命令行 → 检查仓库 → 创建 Agent → 运行并输出状态。
# 接着阅读 core/state.py 和 core/agent.py，可先掌握整体流程。
import argparse
import json
import sys
from pathlib import Path
from issue_agent.core.agent import Agent
from issue_agent.core.model import AnthropicModelClient
from issue_agent.workspace.workspace import Workspace
from issue_agent.workspace.repository import inspect_repository


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue Agent — Phase 4: verified changes and local PR artifacts")
    #创建一个解析器
    parser.add_argument("--repo", required=True, help="Local Git repository root")
    issue_source = parser.add_mutually_exclusive_group(required=True)
    issue_source.add_argument("--issue", help="Task description")
    issue_source.add_argument("--issue-file", type=Path, help="UTF-8 task file (recommended for PowerShell)")
    parser.add_argument("--model")
    parser.add_argument("--verification-model", help="Optional reviewer model; defaults to execution model")
    parser.add_argument("--test-timeout", type=int, default=300, help="Test timeout in seconds (1–1800)")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--inspect", action="store_true", help="Inspect repository without calling a model or editing files")
    args = parser.parse_args()
    try:
        # 直接读取文件，避免旧版 PowerShell 拆坏任务文字中的引号或换行。
        issue = args.issue_file.read_text(encoding="utf-8-sig") if args.issue_file else args.issue
        if not issue.strip():
            raise ValueError("Issue must not be empty")
        # --repo 是被修改的目标仓库，不是本程序自身所在的目录。
        workspace = Workspace(args.repo)
        repository = inspect_repository(workspace)
        # 只检查仓库时不创建模型客户端，也不进入修改代码的循环。
        if args.inspect:
            print(json.dumps(repository, ensure_ascii=False, indent=2))
            return 0
        model = AnthropicModelClient(args.model)
        reviewer = AnthropicModelClient(args.verification_model) if args.verification_model else model
        agent = Agent(workspace, issue, model, args.max_steps,
                      test_timeout=args.test_timeout, verification_model=reviewer)
        state = agent.run()
        print(json.dumps(agent.result, ensure_ascii=False, indent=2))
        # 退出码：0 表示真正完成；2 表示未完成；下面的 1 表示启动等异常。
        return 0 if state.status == "completed" else 2
    except Exception as exc:
        print(f"Issue Agent: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
