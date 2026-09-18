import json
import re
from ..core.state import AgentState
from ..trace.tracer import Tracer
from ..workspace.changes import RepositoryChanges


class ResultBuilder:
    """验证通过后整理交付物。文案来自任务和证据，不额外调用模型。"""

    def __init__(self, changes: RepositoryChanges, tracer: Tracer) -> None:
        self.changes, self.tracer = changes, tracer

    @staticmethod
    #把类里的方法变成一个“不需要 self 的普通工具函数”
    def reviewed_summary(state: AgentState) -> str | None:
        """复用最后一次成功审查的说明，不把执行模型自称完成的文字当作证据。

        兼容旧记录或自定义验证器；缺少结构化结论时保留基础模板。
        """
        if not state.verification_results or not state.verification_results[-1].get("passed"):
            return None
        try:
            review = json.loads(state.verification_results[-1].get("feedback", ""))
        except (TypeError, ValueError):
            return None
        if not isinstance(review, dict) or review.get("completed") is not True:
            return None
        reason = review.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return None
        # 使用引用块标明模型审查说明的来源，避免多行文本改变后续证据章节。
        return "\n".join("> " + line for line in reason.strip().splitlines())

    def build(self, state: AgentState) -> None:
        if not state.verification_results or not state.verification_results[-1]["passed"]:
            raise ValueError("Cannot produce PR artifacts before verification passes")
        latest = state.test_results[-1] if state.test_results else None
        if not latest or not latest["passed"] or latest["fingerprint"] != self.changes.fingerprint():
            raise ValueError("Cannot deliver: missing passing tests or sources changed after tests")
        names, patch = self.changes.diff()
        if not names or not patch:
            raise ValueError("Cannot deliver an empty task diff")
        # 标题保持简单、确定性；不根据文件名猜测 feat/fix 等语义。
        heading = next((line.strip().lstrip("# ") for line in state.task.splitlines() if line.strip().lstrip("# ")), "Update repository")
        title = re.sub(r"\s+", " ", heading)
        title = title if len(title) <= 100 else title[:97] + "..."
        passed = latest["tests"] - latest["failures"] - latest["errors"] - latest["skipped"]
        lines = ["## Summary", f"- Task: {title}"]
        summary = self.reviewed_summary(state)
        if summary:
            lines.extend(["", "Model-reviewed change summary:", "", summary])
        lines.extend(["", "Changes below are relative to the start of this task; pre-existing edits are excluded.",
                      "", "## Changed files"])
        lines.extend(f"- `{name}`" for name in names)
        lines.extend(["", "## Verification", "- Deterministic checks and requirement review passed.",
                      f"- Final test run: {latest['tests']} total, {passed} passed, "
                      f"{latest['failures']} failed, {latest['errors']} errors, {latest['skipped']} skipped.",
                      "- Executed command:", "```text", " ".join(latest["command"]), "```",
                      "", "## Test history", "| Run | Total | Failures | Errors | Skipped | Result |",
                      "| --- | --- | --- | --- | --- | --- |"])
        for i, test in enumerate(state.test_results, 1):
            lines.append(f"| {i} | {test['tests']} | {test['failures']} | {test['errors']} | "
                         f"{test['skipped']} | {'passed' if test['passed'] else 'failed'} |")
        lines.extend(["", "## Review notes", "- Generated locally; no commit, push or GitHub PR was created.",
                      "- Local evidence: `task.diff`, `git.diff`, `state.json`, `metrics.json` and `test-*.log`."])
        description = "\n".join(lines) + "\n"
        state.diff_path = self.tracer.save("task.diff", patch)
        # 完整内容先写入，最后才把产物引用挂到状态里，失败任务不会发布成功文案。
        title_path = self.tracer.save("pr-title.txt", title + "\n")
        description_path = self.tracer.save("pr-description.md", description)
        state.pr_title, state.pr_description = title, description
        state.changed_files = names
        state.artifacts = {"task_diff": state.diff_path, "git_diff": f"{self.tracer.relative}/git.diff",
                           "pr_title": title_path, "pr_description": description_path}
        self.tracer.record("ARTIFACTS_CREATED", state.step_count, artifacts=state.artifacts)


def result_payload(state: AgentState, metrics: dict, output_directory: str) -> dict:
    """CLI 与 result.json 共享同一份结果，便于以后接 Benchmark。"""
    return {"status": state.status, "error": state.error, "task": state.task,
            "repo_path": state.repo_path, "files_modified": state.files_modified,
            "changed_files": state.changed_files, "response": state.final_response,
            "last_test": state.test_results[-1] if state.test_results else None,
            "diff_path": state.diff_path, "pr_title": state.pr_title,
            "pr_description": state.pr_description, "artifacts": state.artifacts,
            "metrics": metrics, "outputs": output_directory}
