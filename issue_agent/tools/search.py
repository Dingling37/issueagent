from itertools import islice
from .file import FileTools


# 纯 Python 的文件列举与字面文本搜索，不给模型开放 shell，也不使用正则表达式。
class SearchTools:
    def __init__(self, files: FileTools) -> None:
        self.files = files

    def list_files(self) -> str:
        # 多取一个用于判断是否超限，但最多向结果写入 2000 个文件名。
        paths = list(islice(self.files.workspace.files(), 2001))
        return "\n".join(paths[:2000]) + ("\n[limited to 2000 files]" if len(paths) > 2000 else "")

    def search_code(self, query: str) -> str:
        if not query or len(query) > 1000:
            raise ValueError("Use a literal query of 1–1000 characters")
        hits: list[str] = []
        for index, path in enumerate(self.files.workspace.files()):
            if index >= 10000:
                return "\n".join(hits) + "\n[scan limited to 10000 files]"
            # 共用 FileTools 的路径检查和文本大小限制；二进制等不适合搜索的文件跳过。
            try:
                content = self.files.text(path)
            except (ValueError, OSError):
                continue
            for line, text in enumerate(content.splitlines(), 1):
                # 返回 路径:行号:片段，模型可以随后用 read_file 阅读上下文。
                if query in text:
                    hits.append(f"{path}:{line}: {text[:500]}")
                    if len(hits) >= 100:
                        return "\n".join(hits) + "\n[limited to 100 matches]"
        return "\n".join(hits) or "No matches"
