import json
import re
import subprocess
from ..core.state import AgentState
from ..trace.tracer import Tracer
from ..workspace.changes import RepositoryChanges


class ResultBuilder:
    """验证通过后整理交付物。文案来自任务和证据，不额外调用模型。"""

    def __init__(self, changes: RepositoryChanges, tracer: Tracer) -> None:
        self.changes, self.tracer = changes, tracer

    def committed_diff(self, state: AgentState) -> tuple[list[str], str]:
        info = state.git_task
        if info.get("commit_verified") != "true":
            raise ValueError("Cannot deliver local task before commit verification")
        base, sha = info.get("base_commit"), info.get("commit_sha")
        if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)
                   for value in (base, sha)) or not info.get("task_branch"):
            raise ValueError("Cannot deliver: missing or invalid commit range")

        def git(*args: str) -> str:
            result = subprocess.run(["git", "--no-pager", *args], cwd=self.changes.workspace.root,
                                    capture_output=True, timeout=30)
            if result.returncode:
                raise ValueError("Cannot read committed delivery: " + result.stderr.decode("utf-8", errors="replace"))
            return result.stdout.decode("utf-8")

        if git("rev-list", "--parents", "-n", "1", sha).split() != [sha, base]:
            raise ValueError("Cannot deliver: commit parent differs from base commit")
        # 固定 SHA 两端，不使用 HEAD 或工作区；禁用外部 diff、textconv 和重命名合并。
        options = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color")
        names = git(*options, "--name-only", "-z", base, sha, "--").split("\0")
        patch = git(*options, "--binary", "--full-index", "--src-prefix=a/", "--dst-prefix=b/", base, sha, "--")
        return [name for name in names if name], patch

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
        if state.git_task and state.git_task.get("commit_verified") != "true":
            raise ValueError("Cannot deliver local task before commit verification")
        if not state.verification_results or not state.verification_results[-1]["passed"]:
            raise ValueError("Cannot produce PR artifacts before verification passes")
        latest = state.test_results[-1] if state.test_results else None
        if not latest or not latest["passed"] or latest["fingerprint"] != self.changes.fingerprint():
            raise ValueError("Cannot deliver: missing passing tests or sources changed after tests")
        local_commit = bool(state.git_task)
        names, patch = self.committed_diff(state) if local_commit else self.changes.diff()
        if not names or not patch:
            raise ValueError("Cannot deliver an empty task diff")
        task_path = f"{self.tracer.relative}/task.diff"
        if local_commit and not (self.tracer.directory / "task.diff").is_file():
            raise ValueError("Cannot deliver: missing Verifier task.diff evidence")
        # 标题保持简单、确定性；不根据文件名猜测 feat/fix 等语义。
        heading = next((line.strip().lstrip("# ") for line in state.task.splitlines() if line.strip().lstrip("# ")), "Update repository")
        title = re.sub(r"\s+", " ", heading)
        title = title if len(title) <= 100 else title[:97] + "..."
        passed = latest["tests"] - latest["failures"] - latest["errors"] - latest["skipped"]
        lines = ["## Summary", f"- Task: {title}"]
        summary = self.reviewed_summary(state)
        if summary:
            lines.extend(["", "Model-reviewed change summary:", "", summary])
        if local_commit:
            lines.extend(["", "## Commit range", f"- Task branch: `{state.git_task['task_branch']}`",
                          f"- Base commit: `{state.git_task['base_commit']}`",
                          f"- Commit SHA: `{state.git_task['commit_sha']}`",
                          "- Official diff: `commit.diff` (base commit → commit SHA).",
                          "- Verifier workspace snapshot evidence: `task.diff`."])
        else:
            lines.extend(["", "Changes below are relative to the start of this task; pre-existing edits are excluded."])
        lines.extend(["", "## Changed files"])
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
        commit_note = (f"- Verified local commit: `{state.git_task['commit_sha']}`. No push or GitHub PR was created."
                       if state.git_task.get("commit_verified") == "true"
                       else "- Generated locally; no commit, push or GitHub PR was created.")
        lines.extend(["", "## Review notes", commit_note,
                      "- Local evidence: `task.diff`, `git.diff`, `state.json`, `metrics.json` and `test-*.log`."])
        description = "\n".join(lines) + "\n"
        diff_path = self.tracer.save("commit.diff" if local_commit else "task.diff", patch)
        # 完整内容先写入，最后才把产物引用挂到状态里，失败任务不会发布成功文案。
        title_path = self.tracer.save("pr-title.txt", title + "\n")
        description_path = self.tracer.save("pr-description.md", description)
        state.pr_title, state.pr_description = title, description
        state.changed_files = names
        state.diff_path = diff_path
        state.delivery_diff = {"source": "git_commit_range" if local_commit else "workspace_snapshot",
                               "base_commit": state.git_task.get("base_commit") if local_commit else None,
                               "commit_sha": state.git_task.get("commit_sha") if local_commit else None,
                               "path": diff_path}
        state.artifacts = {"task_diff": task_path, "git_diff": f"{self.tracer.relative}/git.diff",
                           "official_diff": diff_path,
                           "pr_title": title_path, "pr_description": description_path}
        self.tracer.record("ARTIFACTS_CREATED", state.step_count, artifacts=state.artifacts)


def result_payload(state: AgentState, metrics: dict, output_directory: str) -> dict:
    """CLI 与 result.json 共享同一份结果，便于以后接 Benchmark。"""
    return {"status": state.status, "error": state.error, "task": state.task,
            "repo_path": state.repo_path, "git_task": state.git_task,
            "files_modified": state.files_modified,
            "changed_files": state.changed_files, "response": state.final_response,
            "last_test": state.test_results[-1] if state.test_results else None,
            "diff_path": state.diff_path, "delivery_diff": state.delivery_diff, "pr_title": state.pr_title,
            "pr_description": state.pr_description, "artifacts": state.artifacts,
            "metrics": metrics, "outputs": output_directory}
