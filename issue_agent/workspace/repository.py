# 这里只做启动检查：确认 Git 根目录、识别构建类型，不运行测试。
import subprocess
from .workspace import Workspace


def inspect_repository(workspace: Workspace) -> dict[str, object]:
    # 固定的只读 Git 命令，由程序执行；模型不能通过工具传入任意命令。
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=workspace.root,
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    if result.returncode or workspace.root != type(workspace.root)(result.stdout.strip()).resolve():
        raise ValueError("--repo must identify the root of a local Git repository")
    # 目前仅依据根目录文件识别 Maven/Gradle，结果供 --inspect 展示。
    build = "unknown"
    for filename, kind in (("pom.xml", "maven"), ("build.gradle", "gradle"), ("build.gradle.kts", "gradle")):
        if workspace.resolve(filename).is_file():
            build = kind
            break
    return {"root": str(workspace.root), "build_system": build}
