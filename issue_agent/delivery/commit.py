"""框架专用本地提交；不向模型开放 Git 写权限。"""
import subprocess

from ..core.state import AgentState
from ..workspace.changes import RepositoryChanges


def commit_verified_task(state: AgentState, changes: RepositoryChanges) -> None:
    def git(*args: str, data: bytes | None = None) -> bytes:
        result = subprocess.run(["git", "--literal-pathspecs", *args], cwd=changes.workspace.root,
                                input=data, capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError("Local commit failed: " + result.stderr.decode("utf-8", errors="replace").strip())
        return result.stdout

    info = state.git_task
    if not state.verification_results or not state.verification_results[-1]["passed"]:
        raise ValueError("Cannot commit before verification passes")
    fingerprint = changes.fingerprint()
    if not state.test_results or not state.test_results[-1]["passed"] or state.test_results[-1]["fingerprint"] != fingerprint:
        raise ValueError("Cannot commit: tests do not match current sources")
    base, branch = info["base_commit"], info["task_branch"]
    if git("rev-parse", "HEAD").decode().strip() != base or git("branch", "--show-current").decode().strip() != branch:
        raise ValueError("Cannot commit: task branch or base HEAD changed")
    if git("diff", "--cached", "--name-only", "-z"):
        raise ValueError("Cannot commit: unexpected staged changes")
    names, _ = changes.diff()
    if not names or not set(names).issubset(state.files_modified):
        raise ValueError("Cannot commit: changes include files not modified by this task")
    verified = changes.snapshot()
    # 明确列出路径，包含新增/删除；绝不使用 git add .，不暂存日志和构建目录。
    for name in names:
        git("add", "--", name)
    staged = set(git("diff", "--cached", "--no-renames", "--name-only", "-z", base).decode("utf-8").strip("\0").split("\0"))
    if staged != set(names):
        raise ValueError("Cannot commit: staged file set differs from verified changes")
    for name in names:
        if name not in verified:
            continue  # 删除已由上面的差异文件集合检查。
        # 按仓库 clean/EOL 规则计算 Git blob，兼容 Windows CRLF 正规化。
        expected = git("hash-object", "--path=" + name, "--stdin", data=verified[name]).strip()
        actual = git("rev-parse", ":" + name).strip()
        if expected != actual:
            raise ValueError(f"Cannot commit: staged content differs from verified file: {name}")
    tree = git("write-tree").strip()
    if changes.fingerprint() != fingerprint:
        raise ValueError("Cannot commit: sources changed while staging")
    # 确定性的提交说明；SHA 仍包含 Git 作者和时间，不承诺跨运行相同。
    git("commit", "-m", "Implement verified issue", "--no-gpg-sign")
    sha = git("rev-parse", "HEAD").decode().strip()
    info["commit_sha"] = sha
    info["commit_verified"] = "false"
    if (git("rev-parse", "HEAD^{tree}").strip() != tree
            or git("rev-list", "--parents", "-n", "1", "HEAD").decode().split() != [sha, base]
            or git("branch", "--show-current").decode().strip() != branch
            or git("diff", "--cached", "--name-only", "-z")
            or changes.fingerprint() != fingerprint):
        raise ValueError("Commit consistency check failed; commit retained for inspection")
    info["commit_verified"] = "true"
