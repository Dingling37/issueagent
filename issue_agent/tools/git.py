import subprocess
from ..workspace.workspace import Workspace


class GitTools:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _run(self, arguments: list[str]) -> str:
        result = subprocess.run(["git", "--no-pager", *arguments], cwd=self.workspace.root,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if result.returncode:
            raise ValueError(result.stderr.strip() or "Git command failed")
        return result.stdout

    def status(self) -> str:
        return self._run(["status", "--short", "--untracked-files=all", "--", ".", ":(exclude).task_outputs/**"])

    def diff(self) -> str:
        # 禁用 external diff/textconv，防止读取 diff 时运行仓库自定义程序。
        scope = ["--", ".", ":(exclude).task_outputs/**"]
        return (self._run(["diff", "--no-ext-diff", "--no-textconv", *scope]) +
                self._run(["diff", "--cached", "--no-ext-diff", "--no-textconv", *scope]))
