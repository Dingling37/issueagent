from ..core.state import AgentState


class Planner:
    """先用可解释的固定阶段规划；Issue 和构建类型会进入具体任务文字。"""

    def create(self, state: AgentState, repository: dict) -> list[dict]:
        build = repository.get("build_system", "unknown")
        tasks = [
            f"搜索并阅读与 Issue 相关的代码：{state.task}",
            "实现 Issue 要求，补充相关正常和异常路径测试",
            f"运行 {build} 测试，根据失败结果修复并重新测试",
            "检查任务差异，提交 Verifier 判断需求是否完整实现",
        ]
        return [{"id": i, "task": task, "status": "in_progress" if i == 1 else "pending"}
                for i, task in enumerate(tasks, 1)]

    def advance(self, state: AgentState, step: int) -> None:
        # 这里表示正在经历哪个阶段，不等于已经证明需求完成。
        state.current_step = step
        for item in state.plan:
            item["status"] = "completed" if item["id"] < step else (
                "in_progress" if item["id"] == step else "pending")
