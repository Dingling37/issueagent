"""执行上下文专用投影：保留修复线索，不改动完整测试记录。"""
import re


MAX_DIAGNOSTIC_CHARS = 2000


def _external_path(match: re.Match) -> str:
    value = match.group(0).strip("\"'").replace("\\", "/").rstrip("/")
    parts = value.split("/")
    # 外部路径保留文件名；JDK 的 jre/bin 还需保留上级发行版身份。
    count = 2 if parts[-1].lower() in ("jre", "bin") else 1
    return "<external>/" + "/".join(parts[-count:])

#绝对路径变短
def _clean(text: str, repo_path: str) -> str:
    for root in (repo_path, repo_path.replace("\\", "/")):
        if root:
            text = text.replace(root.rstrip("/\\") + "\\", "")
            text = text.replace(root.rstrip("/\\") + "/", "")
            text = text.replace(root, "<repo>")
    # 引号包含的绝对路径允许空格；不吞掉引号外的错误消息。
    text = re.sub(r"[\"'](?:[A-Za-z]:[\\/]|/|\\\\)[^\r\n\"']*[\"']", _external_path, text)
    # Windows 源码路径保留冒号后的行号和错误文字。
    text = re.sub(r"(?:[A-Za-z]:[\\/]|\\\\)[^\r\n<>\"|]*?\.(?:java|kt|xml|log)(?=[:\s\])]|$)",
                  _external_path, text, flags=re.IGNORECASE)
    text = re.sub(r"[A-Za-z]:[\\/][^\r\n<>\"|]*?(?=:\s|$)", _external_path, text, flags=re.MULTILINE)
    text = re.sub(r"\\\\[^\s\"<>|]+", _external_path, text)
    text = re.sub(r"(?<![\w>/])/(?:[^\s:;,\"'()]+/?)+", _external_path, text)
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    # 两端保留，避免长外层描述挤掉末尾的根因或 expected/actual。
    head = (limit - 5) // 2
    return text[:head] + " ... " + text[-(limit - 5 - head):]


def short_failure(text: str, repo_path: str, limit: int = 300) -> str:
    return _clip(_clean(text, repo_path), limit)


def _diagnostics(tail: str) -> tuple[list[str], bool]:
    lines = tail.splitlines()
    # Gradle 的连续原因链比孤立关键词可靠；帮助段不属于错误原因。
    for start, line in enumerate(lines):
        if "What went wrong:" in line:
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].strip().startswith(("* Try:", "* Exception is:", "* Get more help"))), len(lines))
            block = "\n".join(lines[start + 1:end]).strip()
            if block:
                return [block], False
    pattern = r"\berror\b|cannot find symbol|compilation failure|错误|找不到符号"
    indices = [i for i, line in enumerate(lines) if re.search(pattern, line, re.IGNORECASE)]
    if indices:
        blocks = []
        omitted = len(indices) > 3
        for position, start in enumerate(indices[:3]):
            next_error = indices[position + 1] if position + 1 < len(indices) else len(lines)
            # 包含 symbol/location 等紧邻诊断，且不重复收集下一个错误块。
            blocks.append("\n".join(lines[start:min(start + 6, next_error)]))
            omitted = omitted or next_error > start + 6
        return blocks, omitted
    # 未识别错误也要留下证据；不据日志中的数字推断根因。
    return ([tail.strip()] if tail.strip() else []), False


def summarize_test(result: dict, repo_path: str) -> dict:
    fields = ("step", "passed", "returncode", "timed_out", "tests", "failures", "errors", "skipped")
    summary = {key: result.get(key) for key in fields}
    if result.get("passed"):
        return summary
    truncated = False
    budget = MAX_DIAGNOSTIC_CHARS

    def take(text: str, limit: int) -> str:
        nonlocal budget, truncated
        clean = _clean(text, repo_path)
        limit = min(limit, budget)
        truncated = truncated or len(clean) > limit
        output = _clip(clean, limit)
        budget -= len(output) + 1
        return output

    if result.get("reason"):
        summary["reason"] = take(str(result["reason"]), 300)
    details = result.get("failure_details", [])
    if details:
        summary["failure_details"] = [take(str(item), 300) for item in details[:3]]
        summary["omitted_failure_details"] = max(0, len(details) - 3)
        truncated = truncated or len(details) > 3
    else:
        blocks, omitted = _diagnostics(str(result.get("output_tail", "")))
        if blocks:
            # 单个构建原因段/兜底最多 800 字符，多段编译错误各最多 500。
            summary["diagnostics"] = [take(block, 800 if len(blocks) == 1 else 500) for block in blocks]
        truncated = truncated or omitted
    summary["details_truncated"] = truncated
    return summary
