from time import monotonic
from dataclasses import asdict
from .state import AgentState
from .recovery import BudgetGuard, BudgetLimits, RecoveryPolicy, FailureClassifier, FailureKind, RecoveryError, StateConflict
from copy import deepcopy
from ..security import redact
from .model import ModelClient, ModelResponse
from ..context.manager import ContextManager
from ..tools.base import Tool, Permission
from ..tools.file import FileTools
from ..tools.search import SearchTools
from ..tools.registry import ToolRegistry
from ..trace.tracer import Tracer
from ..workspace.workspace import Workspace
from ..verification.verifier import Verifier, PendingVerifier, IssueVerifier
from ..verification.submission import SUBMIT_VERIFICATION, VERIFICATION_TOOL_CHOICE
from ..planning.planner import Planner
from ..delivery.result import ResultBuilder, result_payload
from ..delivery.commit import commit_verified_task
import json
from ..tools.test import TestRunner
from ..tools.git import GitTools
from ..workspace.changes import RepositoryChanges
from ..workspace.repository import inspect_repository


# 总调度，重构自 s01 的 agent_loop 思想。工具实现放在 tools/，模型调用放在 model.py。
class Agent:
    def __init__(self, workspace: Workspace, task: str, model: ModelClient,
                 max_steps: int = 20, verifier: Verifier | None = None,
                 test_timeout: int = 300, verification_model: ModelClient | None = None,
                 tracer: Tracer | None = None, budget: BudgetLimits | None = None,
                 recovery_policy: RecoveryPolicy | None = None) -> None:
        if not task.strip() or len(task) > 20000:
            raise ValueError("Issue must contain 1–20000 characters")
        if not 1 <= max_steps <= 200:
            raise ValueError("max_steps must be between 1 and 200")
        # 同一个任务的工具、日志、上下文管理器共享这份 state。
        self.state = AgentState(task=task, repo_path=str(workspace.root))
        self.model, self.max_steps = model, max_steps
        self.tracer = tracer or Tracer(workspace)
        self.context = ContextManager()
        self.planner = Planner()
        self.changes = RepositoryChanges(workspace)
        self.runner = TestRunner(workspace, self.state, self.tracer, self.changes, test_timeout)
        self.git = GitTools(workspace)
        self.verification_model = verification_model or model
        self.verifier = verifier or IssueVerifier(self.changes, self.runner, self.git, self.tracer,
            lambda context, budget: self.call_model(context, [SUBMIT_VERIFICATION], "verification", max_tokens=budget))
        self.workspace = workspace
        self.custom_verifier = verifier is not None
        self.metrics: dict = {}
        self.result: dict = {}
        self.persistence = None
        self.budget = BudgetGuard(self.state, budget)
        self.recovery_policy = recovery_policy or RecoveryPolicy()
        self.safe_state = None
        self.safe_phase = "execution"
        self.resume_phase = None
        self.in_verification = False
        self.runner.on_complete = lambda: self.checkpoint("verification") if self.in_verification else None
        self.runner.budget_guard = self.budget
        self.registry = ToolRegistry(self.state, self.tracer)
        self.registry.budget_guard = self.budget
        files = FileTools(workspace, self.state, self.tracer)
        search = SearchTools(files)
        # 相当于教学代码的 TOOLS + TOOL_HANDLERS，但现在每项同时声明参数和处理函数。
        # 模型只能调用注册工具；run_test 无参数，不接受任意命令。
        for tool in [
            Tool("run_test", "Run all Maven/Gradle unit tests; no arguments", {}, self.runner.run_tool, Permission.EXECUTE),
            Tool("git_status", "Read Git working tree status", {}, self.git.status),
            Tool("git_diff", "Read tracked Git diffs; untracked new files are not included", {}, self.git.diff),
            Tool("list_files", "List repository files, excluding generated directories", {}, search.list_files),
            Tool("read_file", "Read a UTF-8 file, at most 400 lines", {"path": str, "start_line": int, "end_line": int}, files.read_file),
            Tool("search_code", "Search literal text in repository files", {"query": str}, search.search_code),
            Tool("edit_file", "Modify an existing UTF-8 file, including adding tests to an existing test class. Read the current file with read_file first, then provide old_text that uniquely matches the content to replace. Use this tool even when replacing the entire contents of an existing file.", {"path": str, "old_text": str, "new_text": str}, files.edit_file, Permission.WRITE, mutates_workspace=True),
            Tool("create_file", "Create a new UTF-8 file only at a path that does not exist. Check repository files before choosing this tool. Never use it for an existing file, even to replace its entire contents; use read_file followed by edit_file instead. Existing files are never overwritten.", {"path": str, "content": str}, files.create_file, Permission.WRITE, mutates_workspace=True),
        ]:
            self.registry.register(tool)

    def _call_model_once(self, context: list[dict], tools: list[dict], purpose: str, *, max_tokens: int = 4096) -> ModelResponse:
        # 执行与验证调用都走这里，统一统计 token、耗时和错误。
        state = self.state
        start = monotonic()
        self.budget.consume("llm_calls")
        # DeepSeek 思考模式不支持强制工具选择；仅审查请求关闭，执行请求不传此参数。
        options = {"tool_choice": VERIFICATION_TOOL_CHOICE,
                   "thinking": {"type": "disabled"}} if purpose == "verification" else {}
        self.tracer.record("LLM_CALL", state.step_count, purpose=purpose, context=context,
                           tools=tools, max_tokens=max_tokens, **options)
        client = self.verification_model if purpose == "verification" else self.model
        try:
            response = client.call(context, tools, max_tokens=max_tokens, **options)
        except (Exception, KeyboardInterrupt) as exc:
            self.tracer.record("LLM_ERROR", state.step_count, purpose=purpose, error=str(exc),
                               request_id=getattr(exc, "request_id", None), duration=monotonic()-start)
            if self.persistence:
                self.persistence.model_call(self, purpose, monotonic()-start,
                    status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error")
            raise
        duration = monotonic()-start
        self.tracer.record("LLM_RESULT", state.step_count, purpose=purpose,
                           duration=duration, response=asdict(response))
        state.token_usage["input"] += response.input_tokens
        state.token_usage["output"] += response.output_tokens
        if self.persistence:
            self.persistence.model_call(self, purpose, duration, response)
        return response

    def call_model(self, context, tools, purpose, *, max_tokens=4096):
        for attempt in range(self.recovery_policy.max_retries + 1):
            try:
                return self._call_model_once(context, tools, purpose, max_tokens=max_tokens)
            except Exception as exc:
                kind = FailureClassifier.classify(exc)
                if kind in {FailureKind.AUTH, FailureKind.QUOTA}:
                    raise RecoveryError(kind, redact(str(exc))) from None
                if kind not in {FailureKind.TRANSIENT, FailureKind.RATE_LIMIT}:
                    raise
                if attempt == self.recovery_policy.max_retries:
                    raise RecoveryError(kind, redact(str(exc))) from None
                previous = self.state.status
                self.transition("recovering")
                delay = self.recovery_policy.delay(exc, attempt)
                self.tracer.record("RECOVERY_RETRY", self.state.step_count,
                                   kind=kind.value, attempt=attempt + 1, delay=delay)
                self.budget.wait(delay)
                self.transition(previous)

    def transition(self, status):
        self.state.status = status
        if self.persistence:
            self.persistence.store.set_status(self.persistence.run_id, status)

    def pause(self, status, error):
        self.budget.update_time()
        counters = {k: deepcopy(getattr(self.state, k)) for k in (
            "budget_usage", "tool_calls", "test_runs", "runtime_seconds", "token_usage")}
        if self.safe_state is not None:
            self.state.__dict__.update(deepcopy(self.safe_state))
        self.state.__dict__.update(counters)
        self.state.status, self.state.error = status, redact(error)
        if self.persistence:
            try:
                self.persistence.pause(self, self.safe_phase)
            except Exception as exc:
                self.state.status, self.state.error = "failed", redact(str(exc))
                raise

    def checkpoint(self, phase):
        self.budget.update_time()
        if self.persistence:
            self.persistence.checkpoint(self, phase)
        self.safe_state = deepcopy(self.state.__dict__)
        self.safe_phase = phase

    def execute_pending_tools(self):
        state = self.state
        batch = state.pending_tool_batch
        calls, results = batch["calls"], batch["results"]
        remaining = calls[len(results):]
        self.budget.check("tool_calls", len(remaining))
        self.budget.check("test_runs", sum(c["name"] == "run_test" for c in remaining))
        for call in remaining:
            mutates = self.registry.mutates_workspace(call["name"])
            if mutates:
                self.planner.advance(state, 2)
            elif call["name"] == "run_test":
                self.planner.advance(state, 3)
            output, ok = self.registry.execute(call["name"], call["input"])
            results.append({"type": "tool_result", "tool_use_id": call["id"],
                            "content": output, "is_error": not ok})
            # The handler, output recording, and result confirmation precede persistence.
            # Persistence failures escape before any following tool is invoked.
            if ok and mutates:
                self.checkpoint("execution")
        state.messages.append({"role": "user", "content": results})
        state.pending_tool_batch = {}
        self.checkpoint("execution")

    def run(self) -> AgentState:
        state = self.state
        self.budget.started = self.budget.clock()
        self.budget.elapsed = state.runtime_seconds
        state.error = None
        state.status = "running"
        try:
            self.transition("running")
            # 生产任务必须是 Git 根目录；基础测试可显式注入验证器来单测循环。
            if not self.resume_phase:
                state.repository = {} if self.custom_verifier else inspect_repository(self.workspace)
                state.plan = self.planner.create(state, state.repository)
                self.planner.advance(state, 1)
                self.tracer.record("PLAN_CREATED", state.step_count, plan=state.plan)
                self.checkpoint("execution")
            while state.step_count < self.max_steps or state.pending_tool_batch or self.resume_phase in {"verification", "delivery", "artifacts"}:
                if state.pending_tool_batch:
                    self.execute_pending_tools()
                    continue
                if self.resume_phase not in {"verification", "delivery", "artifacts"}:
                    state.step_count += 1
                    # ① 组装本轮输入：任务信息 + 状态摘要 + 近期完整交互。
                    context = self.context.build(state)
                    # ② 模型返回文字或工具调用，验证阶段也使用相同的调用记录入口。
                    response = self.call_model(context, self.registry.schemas(), "execution")
                    # 截断的回复可能含不完整编辑，不能直接执行；提示模型缩小下一次响应。
                    if response.stop_reason == "max_tokens":
                        state.retry_count += 1
                        state.messages.append({"role": "user", "content": "Response truncated. Use a smaller edit or response."})
                        self.checkpoint("execution")
                        continue
                    if len(response.tool_calls) > 8:
                        raise ValueError("Model exceeded eight tool calls per step")
                    state.messages.append({"role": "assistant", "content": response.content})
                    # ③ 有工具请求：逐个执行。普通工具错误由 Registry 转成反馈，不中断循环。
                    if response.tool_calls:
                        state.pending_tool_batch = {"calls": response.tool_calls, "results": []}
                        self.execute_pending_tools()
                        continue
                    # ④ 没有工具请求也不能直接结束：必须交给 Verifier 判定。
                    state.final_response = "\n".join(b.get("text", "") for b in response.content if b.get("type") == "text")
                    self.planner.advance(state, 4)
                    self.checkpoint("verification")
                self.budget.check()
                committed = self.resume_phase == "artifacts"
                if self.resume_phase in {"delivery", "artifacts"}:
                    from ..verification.verifier import VerificationResult
                    verification = VerificationResult(**state.verification_results[-1])
                else:
                    self.transition("verifying")
                    self.in_verification = True
                    try:
                        verification = self.verifier.verify(state)
                    finally:
                        self.in_verification = False
                    state.verification_results.append(asdict(verification))
                self.resume_phase = None
                self.tracer.record("VERIFICATION_ERROR" if verification.infrastructure_error else (
                    "VERIFICATION_PASSED" if verification.passed else "VERIFICATION_FAILED"),
                                   state.step_count, **asdict(verification))
                if verification.passed:
                    self.transition("delivering")
                    self.checkpoint("artifacts" if committed else "delivery")
                    # 显式注入的测试验证器只用于循环单测；生产必须具备真实交付证据。
                    if not self.custom_verifier:
                        if state.git_task and not committed:
                            commit_verified_task(state, self.changes)
                            self.checkpoint("artifacts")
                        ResultBuilder(self.changes, self.tracer).build(state)
                    state.status = "completed"
                    for item in state.plan:
                        item["status"] = "completed"
                    self.tracer.record("TASK_COMPLETED", state.step_count)
                    self.checkpoint("terminal")
                    return state
                # 接口/格式重试耗尽时，停止任务而非误导执行模型修改正确的代码。
                if verification.infrastructure_error:
                    state.status, state.error = "verification_error", verification.feedback
                    self.checkpoint("terminal")
                    return state
                # 需求验证不通过：反馈给模型，下一轮继续修复。
                self.transition("running")
                state.verification_failures += 1
                self.planner.advance(state, 3)
                state.retry_count += 1
                state.messages.append({"role": "user", "content": verification.feedback})
                self.checkpoint("execution")
                # 仅基础测试显式注入的占位验证器保留旧状态。
                if isinstance(self.verifier, PendingVerifier):
                    state.status, state.error = "verification_unavailable", verification.feedback
                    self.checkpoint("terminal")
                    return state
            state.status, state.error = "failed", "Maximum agent steps reached"
            self.checkpoint("terminal")
        except KeyboardInterrupt:
            self.pause("paused_user", "Task interrupted by user")
        except RecoveryError as exc:
            paused = {FailureKind.AUTH: "paused_auth", FailureKind.QUOTA: "paused_quota",
                      FailureKind.BUDGET: "paused_budget"}.get(exc.kind)
            if paused:
                self.pause(paused, str(exc))
            else:
                state.status, state.error = "failed", redact(str(exc))
                self.checkpoint("terminal")
        except Exception as exc:
            from ..persistence import PersistenceError
            if isinstance(exc, StateConflict):
                self.pause("failed", str(exc))
                raise
            if isinstance(exc, PersistenceError):
                state.status, state.error = "failed", str(exc)
                raise
            state.status, state.error = "failed", redact(f"{type(exc).__name__}: {exc}")
            self.checkpoint("terminal")
            self.tracer.record("TASK_FAILED", state.step_count, error=state.error)
        finally:
            if state.git_task:
                self.tracer.save("git-task.json", json.dumps(state.git_task, ensure_ascii=False, indent=2))
            # 即使上面 return 或发生异常，也执行这里，把最终状态和指标保存下来。
            self.tracer.record("TASK_STOPPED", state.step_count, status=state.status, error=state.error)
            self.metrics = self.tracer.finish(state)
            self.result = result_payload(state, self.metrics, str(self.tracer.directory))
            if self.persistence:
                self.result["run_id"] = self.persistence.run_id
            self.tracer.save("result.json", json.dumps(self.result, ensure_ascii=False, indent=2))
            # Durable terminal/pause status is committed atomically with its checkpoint.
        return state
