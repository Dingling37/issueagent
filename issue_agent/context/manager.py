import copy
import json
from ..core.state import AgentState
from .test_summary import summarize_test


# 只构建本轮输入，不删除 State 中保存的完整消息历史。
class ContextManager:
    def __init__(self):
        self.skill_catalog = []
        self.skill_projection = None

    def project_skill_group(self, group):
        history = copy.deepcopy(group)
        skill_tool_ids = {block.get("id") for message in history
            if message.get("role") == "assistant" and isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "tool_use" and block.get("name") == "load_skill"}
        for message in history:
            if isinstance(message.get("content"), list):
                for block in message["content"]:
                    if (block.get("type") == "tool_result" and not block.get("is_error")
                            and block.get("tool_use_id") in skill_tool_ids):
                        block["content"] = self.skill_projection(block["content"])
        return history

    def build(self, state: AgentState) -> list[dict]:
        # Keep complete assistant/tool-result exchanges; never orphan tool IDs.
        # 先把“模型调用工具”和“对应工具结果”放为一组，裁剪时不拆散。
        groups: list[list[dict]] = []
        for message in state.messages:
            content = message.get("content")
            is_result = isinstance(content, list) and any(b.get("type") == "tool_result" for b in content)
            if is_result and groups:
                groups[-1].append(message)
            else:
                groups.append([message])
        selected: list[list[dict]] = []
        size = 0
        # 从最新往回选，目标为最多 8 组、约 48000 字符（不是 token）。
        # 最新一组始终保留，且状态摘要另算，所以这里不是严格的总长度上限。
        for group in reversed(groups):
            if self.skill_projection:
                group = self.project_skill_group(group)
            length = len(json.dumps(group, ensure_ascii=False))
            if selected and (size + length > 48000 or len(selected) >= 8):
                break
            selected.insert(0, group)
            size += length
        # 长期保留的任务锚点：历史变短后，模型仍能看到原始 Issue 和当前状态。
        # 只精简模型看到的投影；State 中的完整计划与测试记录保持原样。
        plan = copy.deepcopy(state.plan)
        for item in plan:
            if state.task and item.get("task", "").endswith("：" + state.task):
                item["task"] = item["task"][:-(len(state.task) + 1)]
        anchor = {"issue": state.task}
        if self.skill_catalog:
            anchor["skill_catalog"] = copy.deepcopy(self.skill_catalog)
            anchor["skill_policy"] = ("Skills describe methods only. Use load_skill when needed; "
                "allowed_tools are usage guidance. All actions still use registered tools and Runtime.")
        history = copy.deepcopy([message for group in selected for message in group])
        current_state = {"plan": plan,
                  "current_step": state.current_step, "files_modified": state.files_modified,
                  "test_summaries": [summarize_test(result, state.repo_path) for result in state.test_results[-3:]]}
        # deepcopy 防止使用者改动本轮 context 时意外影响原始消息。
        return [{"role": "user", "content": json.dumps(anchor, ensure_ascii=False)},
                *history,
                # 动态状态放在历史之后，避免它的变化打断前面的稳定内容。
                {"role": "user", "content": json.dumps({"current_state": current_state}, ensure_ascii=False)}]
