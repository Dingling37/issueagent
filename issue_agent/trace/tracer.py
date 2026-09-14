import json
from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4
from typing import Any
from ..workspace.workspace import Workspace
from ..core.state import AgentState


# 新增的观察记录模块：模型、工具和任务生命周期共用一个任务日志目录。
class Tracer:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        # 随机任务 ID 隔离不同运行；目录位于目标仓库内，不是 Agent 源码目录。
        self.relative = f".task_outputs/{uuid4().hex}"
        self.directory = workspace.resolve(self.relative, internal=True)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.started = monotonic()
        self.sequence = 0

    def save(self, name: str, content: str) -> str:
        relative = f"{self.relative}/{name}"
        self.workspace.resolve(relative, internal=True).write_text(content, encoding="utf-8")
        return relative

    def record(self, event_type: str, step: int, *, duration: float | None = None, **metadata: Any) -> None:
        # JSONL 每行一个事件；metadata 收纳不同事件特有的数据。
        # timestamp 是 UTC 时间，duration 使用 monotonic 计时，单位为秒。
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), "event_type": event_type,
                 "step": step, "duration": duration, "metadata": metadata}
        path = self.workspace.resolve(f"{self.relative}/events.jsonl", internal=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def output(self, content: str) -> str:
        # 使用程序生成的编号作文件名，不直接使用模型传来的工具调用 ID。
        self.sequence += 1
        return self.save(f"tool-{self.sequence:04d}.log", content)

    def finish(self, state: AgentState) -> dict[str, Any]:
        # 汇总现有状态；tests 当前为 0，因为真实 TestRunner 尚未接入。
        # 注意 verification_failures 暂取 retry_count，也含响应截断重试，尚非纯验证失败数。
        metrics = {"llm_calls": state.llm_calls, "tool_calls": len(state.tool_history),
                   "token_usage": state.token_usage, "tests": len(state.test_results),
                   "tool_failures": sum(not item["ok"] for item in state.tool_history),
                   "verification_failures": state.retry_count,
                   "elapsed_seconds": monotonic() - self.started,
                   "modified_files": len(state.files_modified)}
        self.save("state.json", json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
        self.save("metrics.json", json.dumps(metrics, indent=2))
        return metrics
