from ..workspace.workspace import Workspace
from ..core.state import AgentState
from ..trace.tracer import Tracer


# 对应 s02 的 run_read / run_edit / run_write；读写前必须经过 Workspace。
class FileTools:
    max_bytes = 1_000_000

    def __init__(self, workspace: Workspace, state: AgentState, tracer: Tracer) -> None:
        self.workspace, self.state, self.tracer = workspace, state, tracer

    # 内部读取函数，供 read_file、edit_file 和搜索共用；不直接注册为模型工具。
    def text(self, path: str) -> str:
        target = self.workspace.resolve(path)
        if target.stat().st_size > self.max_bytes:
            raise ValueError("File exceeds 1 MB text limit")
        # 按字节解码，避免自动转换 CRLF，减少编辑时无关的换行差异。
        content = target.read_bytes().decode("utf-8")
        if "\x00" in content:
            raise ValueError("Binary files are not supported")
        return content

    def read_file(self, path: str, start_line: int, end_line: int) -> str:
        # 行号从 1 开始，返回带行号的片段，限制单次阅读规模。
        if not 1 <= start_line <= end_line or end_line - start_line >= 400:
            raise ValueError("Use a valid 1-based range of at most 400 lines")
        content = self.text(path)
        canonical = self.workspace.resolve(path).relative_to(self.workspace.root).as_posix()
        if canonical not in self.state.files_read:
            self.state.files_read.append(canonical)
        self.tracer.record("FILE_READ", self.state.step_count, path=canonical)
        return "\n".join(f"{i}: {line}" for i, line in enumerate(content.splitlines(), 1)
                         if start_line <= i <= end_line)

    # 成功写入后统一更新状态、记录事件；这不是最终 Git diff 检查。
    def modified(self, path: str) -> str:
        canonical = self.workspace.resolve(path).relative_to(self.workspace.root).as_posix()
        if canonical not in self.state.files_modified:
            self.state.files_modified.append(canonical)
        self.tracer.record("FILE_MODIFIED", self.state.step_count, path=canonical)
        return f"Modified {canonical}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        content = self.text(path)
        # 必须唯一匹配：如果有多处相同文字，让模型补充上下文再修改，避免改错位置。
        if not old_text or content.count(old_text) != 1:
            raise ValueError("old_text must match exactly once; read the file and provide more context")
        if old_text == new_text:
            raise ValueError("Edit makes no change")
        updated = content.replace(old_text, new_text, 1)
        if len(updated.encode("utf-8")) > self.max_bytes:
            raise ValueError("Updated file exceeds 1 MB limit")
        self.workspace.resolve(path).write_bytes(updated.encode("utf-8"))
        return self.modified(path)

    def create_file(self, path: str, content: str) -> str:
        if len(content.encode("utf-8")) > self.max_bytes:
            raise ValueError("Content exceeds 1 MB limit")
        target = self.workspace.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # x 表示仅新建，文件已存在就报错；b 表示按字节写入。
        with target.open("xb") as stream:
            stream.write(content.encode("utf-8"))
        return self.modified(path)
