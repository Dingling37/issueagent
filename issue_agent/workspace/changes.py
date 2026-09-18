import difflib
import hashlib
import json
from .workspace import Workspace


class RepositoryChanges:
    """保存任务开始时的源码快照，区分用户已有改动和本次任务改动。"""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.baseline = self.snapshot()

    def snapshot(self) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        total = 0
        for path in self.workspace.files():
            target = self.workspace.resolve(path)
            size = target.stat().st_size
            total += size
            if total > 50_000_000 or len(result) >= 20000:
                raise ValueError("Repository source snapshot exceeds MVP limit (50 MB / 20000 files)")
            result[path] = target.read_bytes()
        return result

    def fingerprint(self) -> str:
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in self.snapshot().items()}
        return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()

    def diff(self) -> tuple[list[str], str]:
        current = self.snapshot()
        names = sorted(name for name in self.baseline.keys() | current.keys()
                       if self.baseline.get(name) != current.get(name))
        chunks = []
        for name in names:
            before, after = self.baseline.get(name, b""), current.get(name, b"")
            try:
                if b"\0" in before or b"\0" in after:
                    raise ValueError("binary")
                old, new = before.decode("utf-8"), after.decode("utf-8")
            except (ValueError, UnicodeError):
                raise ValueError(f"Cannot verify a binary/non-UTF-8 change: {name}")
            # keepends=False 避免不同换行格式影响展示；指纹仍按原始字节计算。
            patch = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(),
                fromfile=f"a/{name}" if name in self.baseline else "/dev/null",
                tofile=f"b/{name}" if name in current else "/dev/null", lineterm=""))
            if not patch:
                patch = f"diff --git a/{name} b/{name}\n[Only file existence or line ending changed]"
            chunks.append(patch)
        return names, "\n\n".join(chunks)
