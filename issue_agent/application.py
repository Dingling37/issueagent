"""Shared local launch flow; Benchmark keeps its isolated runner."""
import json
from dataclasses import asdict
from .security import redact
from .presentation import Presentation
from .core.agent import Agent
from .core.model import AnthropicModelClient
from .workspace.workspace import Workspace
from .workspace.repository import inspect_repository, prepare_task_branch


def run_issue(repo, issue, *, model_name=None, verification_model=None,
              max_steps=20, test_timeout=300, inspect=False,
              model_factory=AnthropicModelClient, agent_factory=Agent,
              observer_factory=None, budget=None):
    if not issue.strip():
        raise ValueError("Issue must not be empty")
    # --repo 是被修改的目标仓库，不是本程序自身所在的目录。
    workspace = Workspace(repo)
    repository = inspect_repository(workspace)
    # 只检查仓库时不创建模型客户端，也不进入修改代码的循环。
    if inspect:
        print(json.dumps(repository, ensure_ascii=False, indent=2))
        return 0
    model = model_factory(model_name)
    reviewer = model_factory(verification_model) if verification_model else model
    # 在创建 Agent/日志和调用模型之前准备任务分支；失败时不启动 Agent。
    if len(issue) > 20000 or not 1 <= max_steps <= 200 or not 1 <= test_timeout <= 1800:
        raise ValueError("Invalid issue length, max steps, or test timeout")
    from .persistence.store import Store
    from .persistence.launch import attach
    store = Store.from_environment()
    if store:
        store.check()  # Fail before creating/changing a task branch.
    from .skills import SkillRegistry
    skills = SkillRegistry()
    catalog = skills.catalog()  # Validate imports before changing the task branch.
    git_task = prepare_task_branch(workspace)
    agent = agent_factory(workspace, issue, model, max_steps,
                  test_timeout=test_timeout, verification_model=reviewer)
    if catalog:
        agent.enable_skills(skills)
    if budget is not None:
        agent.budget.limits = budget
    presentation = Presentation(observer_factory, agent.tracer)
    try:
        agent.state.git_task = git_task
        agent.tracer.record("TASK_BRANCH_CREATED", 0, **git_task)
        agent.tracer.save("git-task.json", json.dumps(git_task, ensure_ascii=False, indent=2))
        if store:
            run_id = attach(store, agent, test_timeout)
            print(f"Run ID: {run_id}")
            with store.lease(run_id):
                state = agent.run()
        else:
            state = agent.run()
    finally:
        presentation.close()
    if not presentation.finish(agent.result):
        print(redact(json.dumps(agent.result, ensure_ascii=False, indent=2)))
    # 退出码：0 表示真正完成；2 表示未完成；下面的 1 表示启动等异常。
    return 0 if state.status == "completed" else 2


def resume_issue(run_id, *, model_factory, observer_factory=None, agent_factory=Agent, budget=None):
    from .persistence.store import Store
    from .persistence.launch import restore
    from .persistence import PersistenceError
    store = Store.from_environment()
    if store is None:
        raise PersistenceError("Resume requires ISSUE_AGENT_DATABASE_URL")
    with store.lease(run_id):
        agent = restore(store, run_id, model_factory, agent_factory)
        if budget is not None:
            from .core.recovery import BudgetLimits
            combined = {**asdict(agent.budget.limits),
                        **{k: v for k, v in asdict(budget).items() if v is not None}}
            agent.budget.limits = BudgetLimits(**combined)
            agent.persistence.settings["budget"] = combined
            store.update_settings(run_id, agent.persistence.settings)
        presentation = Presentation(observer_factory, agent.tracer)
        try:
            state = agent.run()
        finally:
            presentation.close()
        if not presentation.finish(agent.result):
            print(redact(json.dumps(agent.result, ensure_ascii=False, indent=2)))
        return 0 if state.status == "completed" else 2
