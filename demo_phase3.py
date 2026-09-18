"""可重复的失败恢复演示：模型响应固定，但 Git、Java 编译、JUnit 测试都是真的。"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
from issue_agent.core.agent import Agent
from issue_agent.core.model import ModelResponse, ScriptedModelClient
from issue_agent.workspace.workspace import Workspace


ADDED_TESTS = '''    @Test
    public void greetsNullAsGuest() {
        assertEquals("Hello guest", new Greeting().greet(null));
    }

    @Test
    public void greetsEmptyAsGuest() {
        assertEquals("Hello guest", new Greeting().greet(""));
    }

    @Test
    public void greetsBlankAsGuest() {
        assertEquals("Hello guest", new Greeting().greet("   "));
    }

'''
FIX = '''        if (name == null || name.trim().isEmpty()) {
            return "Hello guest";
        }
        return "Hello " + name;'''


def tool(name: str, **arguments) -> ModelResponse:
    return ModelResponse([{"type": "tool_use", "id": "demo-call", "name": name, "input": arguments}])


def create_repository(destination: Path) -> None:
    # 只允许全新目录，不覆盖用户已有仓库；初始提交仅供演示比较，不由 Agent 执行。
    if destination.exists():
        raise ValueError("Demo destination must not exist; choose a new directory")
    source = Path(__file__).parent / "examples" / "java-greeting"
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("target", ".task_outputs"))
    commands = [["git", "init", "-q"], ["git", "add", "--", "."],
                ["git", "-c", "user.name=Issue Agent Demo", "-c", "user.email=demo@example.invalid",
                 "-c", "core.hooksPath=" + str(destination / ".no-hooks"), "-c", "commit.gpgsign=false",
                 "commit", "-qm", "Initial Java fixture"]]
    for command in commands:
        subprocess.run(command, cwd=destination, check=True, capture_output=True)


def run_demo(destination: Path) -> Agent:
    create_repository(destination)
    # 第一轮加测试（旧代码会失败），第二轮修复实现，然后由真实 Verifier 检查证据。
    model = ScriptedModelClient([
        tool("edit_file", path="src/test/java/demo/GreetingTest.java", old_text="    @Test\n", new_text=ADDED_TESTS + "    @Test\n"),
        tool("run_test"),
        tool("edit_file", path="src/main/java/demo/Greeting.java", old_text='        return "Hello " + name;', new_text=FIX),
        tool("run_test"),
        ModelResponse([{"type": "text", "text": "已增加边界测试并修复空姓名处理。"}]),
        # 仅 LLM 审查响应固定，不用于证明真实模型能独立判断需求。
        ModelResponse([{"type": "text", "text": json.dumps({"completed": True,
            "reason": "Demo reviewer: null/empty/blank and normal cases covered", "missing_requirements": []})}]),
    ])
    agent = Agent(Workspace(destination), "null、空字符串、纯空格返回 Hello guest；保留正常姓名行为并增加测试。", model)
    agent.run()
    return agent


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="New demo repository directory")
    args = parser.parse_args()
    agent = run_demo(Path(args.repo).resolve())
    results = agent.state.test_results
    confirmed = (agent.state.status == "completed" and len(results) == 2
                 and not results[0]["passed"] and results[0]["failures"] == 3
                 and results[1]["passed"] and results[1]["tests"] == 4)
    print(json.dumps({"status": agent.state.status, "failure_repair_confirmed": confirmed,
        "tests": [{k: row[k] for k in ("passed", "tests", "failures", "returncode")} for row in results],
        "error": agent.state.error, "outputs": str(agent.tracer.directory)}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if confirmed else 2)
