# 来源：s02 的 safe_path 思想。把全局 WORKDIR 改为每个任务自己的根目录。
import os
from pathlib import Path, PureWindowsPath
from collections.abc import Iterator


class Workspace:
    """File capability boundary; not an OS sandbox for future build processes."""

    excluded = {".git", ".task_outputs", ".venv", ".gradle", ".idea", ".pytest_cache", "node_modules", "__pycache__", "target", "build"}

    def __init__(self, repo_path: str | Path) -> None:
        self.root = Path(repo_path).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Repository must be a directory")

    def resolve(self, path: str, *, internal: bool = False) -> Path:
        # internal 仅供日志模块使用，允许访问 .task_outputs；不会暴露给模型工具。
        normalized = path.replace("\\", "/")
        parts = normalized.split("/")
        if not path or Path(normalized).is_absolute() or PureWindowsPath(path).drive:
            raise ValueError("Use a nonempty repository-relative path")
        if ".." in parts or any(":" in p for p in parts):
            raise ValueError("Parent traversal and alternate data streams are forbidden")
        # 不让模型编辑 Git 元数据或任务日志；Windows 下同时考虑大小写和末尾空格。
        blocked = {".git"} if internal else {".git", ".task_outputs"}
        if any(p.casefold().rstrip(" .") in blocked for p in parts):
            raise ValueError("Protected workspace metadata")
        candidate = self.root / normalized
        # Reject links/junctions even when they currently point inside the repository.
        # 逐级检查路径组件，防止目录链接把访问引到其他位置。
        cursor = self.root
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
                raise ValueError("Symbolic links and junctions are forbidden")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Path escapes workspace")
        # 硬链接也可能对应仓库外文件；拒绝同一文件有多个链接的情况。
        if resolved.is_file() and resolved.stat().st_nlink > 1:
            raise ValueError("Hard-linked files are forbidden")
        return resolved

    def files(self) -> Iterator[str]:
        # os.walk 的 dirs[:] 原地修改会控制下一层遍历，跳过生成目录和链接目录。
        # 这是文件工具边界，不是操作系统沙箱，也不防御并发恶意替换路径。
        for directory, dirs, names in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d.casefold() not in self.excluded
                             and not (Path(directory) / d).is_symlink()
                             and not (hasattr(Path(directory) / d, "is_junction")
                                      and (Path(directory) / d).is_junction()))
            for name in sorted(names):
                relative = (Path(directory) / name).relative_to(self.root).as_posix()
                try:
                    self.resolve(relative)
                except ValueError:
                    continue
                yield relative
