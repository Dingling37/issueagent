"""Rich projection of Tracer events. No Agent, tools, model or Git access."""
import os
import sys
from collections import deque
from threading import RLock
from time import monotonic

from .security import redact


def terminal_text(value, limit=240):
    # Treat all provider/tool strings as literal data, including terminal controls.
    text = redact(str(value))
    return "".join(c for c in text if c in "\n\t" or c.isprintable())[:limit]


class ProgressState:
    """Small bounded event projection; does not retain prompts or tool arguments."""
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.started = clock()
        self.stopped = None
        self.phase = "准备"
        self.tool = "—"
        self.execution_rounds = 0
        self.llm_calls = 0
        self.tool_calls = 0
        self.tokens = {"input": 0, "output": 0}
        self.responses = 0
        self.test = "未运行"
        self.verifier = "未开始"
        self.recent = deque(maxlen=8)

    @property
    def elapsed(self):
        return max(0, (self.stopped if self.stopped is not None else self.clock()) - self.started)

    def observe(self, event):
        kind, data = event["event_type"], event.get("metadata", {})
        label = kind
        if kind == "TASK_BRANCH_CREATED":
            label = "任务分支已创建"
        elif kind == "PLAN_CREATED":
            self.phase, label = "规划", "执行计划已创建"
        elif kind == "LLM_CALL":
            self.llm_calls += 1
            self.tool = "—"
            if data.get("purpose") == "execution":
                self.execution_rounds += 1
                self.phase, label = "Execution", "Execution 模型调用"
            else:
                self.phase, self.verifier = "Verifier", "审查中"
                label = "Verifier 模型调用"
        elif kind == "LLM_RESULT":
            response = data.get("response", {})
            self.responses += 1
            for field in self.tokens:
                value = response.get(field + "_tokens", 0)
                if type(value) is int:
                    self.tokens[field] += value
            label = "模型响应已收到"
            # This only indicates the next phase, never a successful verification.
            if data.get("purpose") == "execution" and response.get("stop_reason") != "max_tokens" \
                    and not any(b.get("type") == "tool_use" for b in response.get("content", [])):
                self.phase, self.verifier = "Verifier", "等待检查结果"
        elif kind == "LLM_ERROR":
            label = "模型调用失败"
            if data.get("purpose") == "verification":
                self.verifier = "调用异常"
        elif kind == "TOOL_CALL":
            self.tool_calls += 1
            self.tool = terminal_text(data.get("name", "unknown"), 60)
            self.phase = "工具执行"
            label = "工具开始：" + self.tool
        elif kind == "TOOL_RESULT":
            label = "工具" + ("完成：" if data.get("ok") else "失败：") + terminal_text(data.get("name", ""), 60)
            self.tool = "—"
            self.phase = "Execution"
        elif kind == "TEST_STARTED":
            self.test, self.phase = "运行中", "测试"
            label = "测试开始"
        elif kind in {"TEST_PASSED", "TEST_FAILED"}:
            result = data.get("result", {})
            self.test = ("通过" if kind == "TEST_PASSED" else "失败") + (
                f" · 总数 {result.get('tests', '?')} / 失败 {result.get('failures', '?')}"
                f" / 错误 {result.get('errors', '?')} / 跳过 {result.get('skipped', '?')}")
            if result.get("timed_out"):
                self.test += " · 超时"
            label = "测试" + ("通过" if kind == "TEST_PASSED" else "失败")
        elif kind in {"VERIFICATION_RETRY", "VERIFICATION_ATTEMPT_FAILED"}:
            self.phase = "Verifier"
            self.verifier = "重试中" if kind == "VERIFICATION_RETRY" else "本次审查无效"
            label = "Verifier：" + self.verifier
        elif kind in {"VERIFICATION_PASSED", "VERIFICATION_FAILED", "VERIFICATION_ERROR"}:
            self.verifier = {"VERIFICATION_PASSED": "通过", "VERIFICATION_FAILED": "未通过",
                             "VERIFICATION_ERROR": "基础设施错误"}[kind]
            self.phase = "交付" if kind == "VERIFICATION_PASSED" else "Verifier"
            label = "Verifier：" + self.verifier
        elif kind == "ARTIFACTS_CREATED":
            self.phase, label = "交付", "本地交付文件已生成"
        elif kind in {"TASK_COMPLETED", "TASK_FAILED", "TASK_STOPPED"}:
            self.phase = terminal_text(data.get("status", "completed" if kind == "TASK_COMPLETED" else "failed"))
            self.tool = "—"
            if kind == "TASK_STOPPED":
                self.stopped = self.clock()
                if self.test == "运行中":
                    self.test = "已停止，未收到测试结果"
                if self.verifier in {"审查中", "等待检查结果", "重试中"}:
                    self.verifier = "已停止，未收到最终结论"
            label = "任务：" + self.phase
        elif kind in {"FILE_READ", "FILE_MODIFIED"}:
            label = ("读取：" if kind == "FILE_READ" else "修改：") + terminal_text(data.get("path", ""), 100)
        else:
            return
        self.recent.append(f"{self.elapsed:6.1f}s  {terminal_text(label, 160)}")


def create_terminal_observer():
    # Do not force a terminal, even when FORCE_COLOR or a CI variable is set.
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)) \
            or os.environ.get("TERM") == "dumb" or os.environ.get("CI"):
        return None
    # Lazy imports ensure Benchmark and the legacy entry point never load Rich.
    from rich.console import Console
    console = Console(file=sys.stderr, markup=False, highlight=False)
    if not console.is_terminal or console.is_dumb_terminal:
        return None
    return RichObserver(console)


class RichObserver:
    def __init__(self, console):
        self.console = console
        self.state = ProgressState()
        self.lock = RLock()
        self.live = None
        self.closed = False

    def render(self):
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        with self.lock:
            state = self.state
            table = Table.grid(padding=(0, 2))
            for key, value in (
                ("当前阶段", state.phase), ("当前工具", state.tool),
                ("Execution 轮数", str(state.execution_rounds)),
                ("LLM / Tool 调用", f"{state.llm_calls} / {state.tool_calls}"),
                ("测试（最近一次）", state.test), ("Verifier", state.verifier),
                ("Token 输入 / 输出", f"{state.tokens['input']} / {state.tokens['output']}（已返回响应）"),
                ("耗时", f"{state.elapsed:.1f}s"),
            ):
                table.add_row(Text(key, style="cyan"), Text(terminal_text(value)))
            return Panel(Group(table, Text("\n最近事件", style="bold"),
                               Text("\n".join(state.recent) or "等待事件…")), title="Issue Agent")

    def start(self):
        from rich.live import Live

        class SafeLive(Live):
            failed = False

            def refresh(self):
                if self.failed:
                    return
                try:
                    super().refresh()
                except Exception:
                    # Also contains failures in Rich's background refresh thread.
                    self.failed = True

        self.live = SafeLive(console=self.console, get_renderable=self.render,
                             refresh_per_second=4, transient=True,
                             redirect_stdout=False, redirect_stderr=False)
        self.live.start(refresh=True)

    def on_event(self, event):
        with self.lock:
            self.state.observe(event)

    def close(self):
        if not self.closed:
            self.closed = True
            if self.live is not None:
                self.live.stop()

    def finish(self, result):
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        if self.live is None or self.live.failed:
            return False
        task = result.get("git_task") or {}
        table = Table.grid(padding=(0, 2))
        for key, value in (
            ("状态", result.get("status", "unknown")),
            ("Branch", task.get("task_branch") or "未创建"),
            ("Commit", task.get("commit_sha") or "未生成"),
            ("Commit 验证", task.get("commit_verified") or "未验证"),
            ("Diff", result.get("diff_path") or "未生成"),
            ("变更文件", ", ".join(result.get("changed_files") or []) or "无"),
            ("PR summary 文件", (result.get("artifacts") or {}).get("pr_description") or "未生成"),
            ("完整结果目录", result.get("outputs", "—")),
        ):
            table.add_row(Text(key, style="cyan"), Text(terminal_text(value, 2000)))
        summary = result.get("pr_description") or "未生成 PR summary。"
        if len(summary) > 6000:
            summary = summary[:6000] + "\n…完整内容见 PR summary 文件。"
        items = [self.render(), table, Text("\nPR summary（本地）", style="bold"),
                 Text(terminal_text(summary, 6200))]
        if result.get("error"):
            items.append(Text(terminal_text(result["error"], 2000), style="red"))
        self.console.print(Panel(Group(*items), title="任务结果"))
        return True
