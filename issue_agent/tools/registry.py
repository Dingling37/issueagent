from ..core.recovery import RecoveryError
from ..persistence import PersistenceError
from sqlalchemy.exc import SQLAlchemyError
import json
from time import monotonic
from typing import Any
from .base import Permission, Tool
from ..core.state import AgentState
from ..trace.tracer import Tracer


# 重构自教学代码的 TOOLS / TOOL_HANDLERS：统一负责工具分发和错误反馈。
class ToolRegistry:
    def __init__(self, state: AgentState, tracer: Tracer) -> None:
        self.state, self.tracer = state, tracer
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        # EXECUTE 只允许无参数的 run_test；危险工具始终禁止。
        if tool.permission == Permission.DANGEROUS or (
            tool.permission == Permission.EXECUTE and (tool.name != "run_test" or tool.parameters)):
            raise ValueError("Only the fixed argument-free run_test command may execute")
        self.tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self.tools.values()]

    def mutates_workspace(self, name: str) -> bool:
        tool = self.tools.get(name)
        return bool(tool and tool.mutates_workspace)

    def execute(self, name: str, arguments: Any) -> tuple[str, bool]:
        # 返回值是 (给模型的结果字符串, 是否成功)，Agent 据此设置 is_error。
        if getattr(self, "budget_guard", None):
            self.budget_guard.consume("tool_calls")
        start = monotonic()
        self.tracer.record("TOOL_CALL", self.state.step_count, name=name, arguments=arguments)
        try:
            if name not in self.tools:
                raise ValueError(f"Unknown or forbidden tool: {name}")
            tool = self.tools[name]
            # 参数必须完整且无多余字段，不能让模型随意传入函数没有声明的参数。
            if not isinstance(arguments, dict) or set(arguments) != set(tool.parameters):
                raise ValueError(f"Expected exactly these arguments: {list(tool.parameters)}")
            for key, kind in tool.parameters.items():
                # 严格检查类型，避免 Python 把 True 也当成整数接受。
                if type(arguments[key]) is not kind:
                    raise ValueError(f"{key} must be {kind.__name__}")
            # **arguments 把字典展开为关键字参数；路径安全由具体文件工具的 Workspace 检查。
            output, ok = tool.handler(**arguments), True
        except (RecoveryError, PersistenceError, SQLAlchemyError):
            raise
        except Exception as exc:
            # 普通工具失败也返回结果，让模型能读到原因并纠正下一次调用。
            output, ok = f"{type(exc).__name__}: {exc}", False
        # 先保存完整输出，再生成头尾摘要；这里没有调用另一个 LLM 做总结。
        output_path = self.tracer.output(output)
        item = {"name": name, "arguments": arguments, "ok": ok, "output_path": output_path}
        self.state.tool_history.append(item)
        self.tracer.record("TOOL_RESULT", self.state.step_count, duration=monotonic() - start, **item)
        summary = output if len(output) <= 6000 else output[:4500] + "\n[truncated]\n" + output[-1000:]
        return json.dumps({"ok": ok, "result": summary, "full_output": output_path}, ensure_ascii=False), ok
