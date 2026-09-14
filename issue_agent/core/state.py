from dataclasses import asdict, dataclass, field
from typing import Any


# 新增的任务状态：不再只依靠教学代码中的 messages 列表。
# dataclass 自动生成构造函数；default_factory 为每个任务创建独立列表/字典。
@dataclass
class AgentState:
    # 原始 Issue 和目标仓库位置，供每轮上下文与日志使用。
    task: str
    repo_path: str
    # 预留：Phase 3 的 Planner 尚未实现，目前 plan 为空。
    plan: list[dict[str, Any]] = field(default_factory=list)
    # 预留的计划步骤位置，不等于下面的模型循环轮数 step_count。
    current_step: int = 0
    # 模型回复和工具结果；ContextManager 从中选取近期交互发送给模型。
    messages: list[dict[str, Any]] = field(default_factory=list)
    # read_file 显式读取过的文件；搜索内部读取不计入此列表。
    files_read: list[str] = field(default_factory=list)
    # 工具曾修改过的文件，不代表最终 diff 一定仍有变化。
    files_modified: list[str] = field(default_factory=list)
    # 工具执行记录与完整输出路径；大段输出本身保存在日志文件中。
    tool_history: list[dict[str, Any]] = field(default_factory=list)
    # 预留：测试执行器尚未接入，当前不会产生真实测试结果。
    test_results: list[dict[str, Any]] = field(default_factory=list)
    # 统计模型 API 返回的输入/输出 token 数量。
    token_usage: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})
    # 每次主循环加一，达到 max_steps 就停止，防止无限运行。
    step_count: int = 0
    # 当前合计响应截断重试和验证拒绝次数，不包含 SDK 内部网络重试。
    retry_count: int = 0
    llm_calls: int = 0
    # 生命周期：pending → running → completed / failed / verification_unavailable。
    status: str = "pending"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # 把 dataclass 转为普通字典，方便 Tracer 保存成 JSON。
        return asdict(self)
