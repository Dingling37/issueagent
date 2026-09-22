import re

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
        if not old_text:
            raise ValueError("old_text must not be empty")
        # 仅将真实 CRLF 视为 LF；空白、缩进和字面量反斜杠仍然精确匹配。
        normalized = content.replace("\r\n", "\n")
        old = old_text.replace("\r\n", "\n")
        new = new_text.replace("\r\n", "\n")
        start = normalized.find(old)
        if start < 0:
            raise ValueError("No match found after normalizing CRLF/LF; read the file again")
        # 从下一字符查找，也拒绝相互重叠的候选匹配。
        if normalized.find(old, start + 1) >= 0:
            raise ValueError("Multiple matches after normalizing CRLF/LF; include more context")
        if old == new:
            raise ValueError("Edit makes no change")
        # 将规范化视图中的边界映射回原文，只替换目标片段，其他字节保持原样。
        def original_offset(position: int) -> int:
            extra = 0
            for match in re.finditer("\r\n", content):
                if match.start() - extra >= position:
                    break
                extra += 1
            return position + extra

        left, right = original_offset(start), original_offset(start + len(old))
        replacement = new
        if "\n" in new:
            if "\r" in normalized or "\r" in new:
                raise ValueError("Standalone CR is unsupported for multiline edits")
            styles = set(re.findall(r"\r?\n", content[left:right]))
            if len(styles) > 1:
                raise ValueError("Matched region has mixed line endings; use a smaller edit")
            if styles:
                newline = next(iter(styles))
            else:
                # 单行插入多行时，沿用该行的结束符；最后一行沿用前一个结束符。
                following = re.search(r"\r?\n", content[right:])
                preceding = re.findall(r"\r?\n", content[:left])
                newline = following.group() if following else (preceding[-1] if preceding else "\n")
            replacement = new.replace("\n", newline)
        updated = content[:left] + replacement + content[right:]
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
