from time import monotonic
from dataclasses import asdict
from .state import AgentState
from .model import ModelClient
from ..context.manager import ContextManager
from ..tools.base import Tool, Permission
from ..tools.file import FileTools
from ..tools.search import SearchTools
from ..tools.registry import ToolRegistry
from ..trace.tracer import Tracer
from ..workspace.workspace import Workspace
from ..verification.verifier import Verifier, PendingVerifier


# 总调度，重构自 s01 的 agent_loop 思想。工具实现放在 tools/，模型调用放在 model.py。
class Agent:
    def __init__(self, workspace: Workspace, task: str, model: ModelClient,
                 max_steps: int = 20, verifier: Verifier | None = None) -> None:
        if not task.strip() or len(task) > 20000:
            raise ValueError("Issue must contain 1–20000 characters")
        if not 1 <= max_steps <= 200:
            raise ValueError("max_steps must be between 1 and 200")
        # 同一个任务的工具、日志、上下文管理器共享这份 state。
        self.state = AgentState(task=task, repo_path=str(workspace.root))
        self.model, self.max_steps = model, max_steps
        # 支持传入验证器以便测试；默认占位验证器绝不会判定任务完成。
        self.verifier = verifier or PendingVerifier()
        self.tracer = Tracer(workspace)
        self.context = ContextManager()
        self.registry = ToolRegistry(self.state, self.tracer)
        files = FileTools(workspace, self.state, self.tracer)
        search = SearchTools(files)
        # 相当于教学代码的 TOOLS + TOOL_HANDLERS，但现在每项同时声明参数和处理函数。
        # 模型只能看到这里注册的五个工具，目前没有 run_test 或 shell。
        for tool in [
            Tool("list_files", "List repository files, excluding generated directories", {}, search.list_files),
            Tool("read_file", "Read a UTF-8 file, at most 400 lines", {"path": str, "start_line": int, "end_line": int}, files.read_file),
            Tool("search_code", "Search literal text in repository files", {"query": str}, search.search_code),
            Tool("edit_file", "Replace one unique exact text match", {"path": str, "old_text": str, "new_text": str}, files.edit_file, Permission.WRITE),
            Tool("create_file", "Create a new UTF-8 file; never overwrite", {"path": str, "content": str}, files.create_file, Permission.WRITE),
        ]:
            self.registry.register(tool)

    def run(self) -> AgentState:
        state = self.state
        state.status = "running"
        try:
            while state.step_count < self.max_steps:
                state.step_count += 1
                # ① 组装本轮输入：任务信息 + 状态摘要 + 近期完整交互。
                context = self.context.build(state)
                start = monotonic()
                state.llm_calls += 1
                self.tracer.record("LLM_CALL", state.step_count, context=context,
                                   tools=self.registry.schemas())
                # ② 把工具说明和上下文交给模型；模型返回文字或 tool_use 请求。
                response = self.model.call(context, self.registry.schemas())
                self.tracer.record("LLM_RESULT", state.step_count, duration=monotonic() - start,
                                   response=asdict(response))
                state.token_usage["input"] += response.input_tokens
                state.token_usage["output"] += response.output_tokens
                # 截断的回复可能含不完整编辑，不能直接执行；提示模型缩小下一次响应。
                if response.stop_reason == "max_tokens":
                    state.retry_count += 1
                    state.messages.append({"role": "user", "content": "Response truncated. Use a smaller edit or response."})
                    continue
                if len(response.tool_calls) > 8:
                    raise ValueError("Model exceeded eight tool calls per step")
                state.messages.append({"role": "assistant", "content": response.content})
                # ③ 有工具请求：逐个执行。普通工具错误由 Registry 转成反馈，不中断循环。
                if response.tool_calls:
                    results = []
                    for call in response.tool_calls:
                        output, ok = self.registry.execute(call["name"], call["input"])
                        # tool_use_id 把结果与模型提出的那一次调用对应起来。
                        results.append({"type": "tool_result", "tool_use_id": call["id"],
                                        "content": output, "is_error": not ok})
                    # 工具结果按 Anthropic 消息格式放在 user 消息里，并非用户亲自输入。
                    state.messages.append({"role": "user", "content": results})
                    continue
                # ④ 没有工具请求也不能直接结束：必须交给 Verifier 判定。
                verification = self.verifier.verify(state)
                self.tracer.record("VERIFICATION_PASSED" if verification.passed else "VERIFICATION_FAILED",
                                   state.step_count, **asdict(verification))
                if verification.passed:
                    state.status = "completed"
                    self.tracer.record("TASK_COMPLETED", state.step_count)
                    return state
                # 验证不通过：原因进入下一轮上下文，模型据此继续工作。
                state.retry_count += 1
                state.messages.append({"role": "user", "content": verification.feedback})
                # Phase 2 特例：真实验证尚未实现，继续重试也无意义，明确停止为“验证不可用”。
                # 将来接入真实 Verifier 后，拒绝结果会沿 while 循环继续反馈。
                if isinstance(self.verifier, PendingVerifier):
                    state.status, state.error = "verification_unavailable", verification.feedback
                    return state
            state.status, state.error = "failed", "Maximum agent steps reached"
        except Exception as exc:
            state.status, state.error = "failed", f"{type(exc).__name__}: {exc}"
            self.tracer.record("TASK_FAILED", state.step_count, error=state.error)
        finally:
            # 即使上面 return 或发生异常，也执行这里，把最终状态和指标保存下来。
            self.tracer.record("TASK_STOPPED", state.step_count, status=state.status, error=state.error)
            self.tracer.finish(state)
        return state
