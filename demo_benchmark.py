"""One deterministic end-to-end benchmark smoke run; it does not call a paid model."""

import json
from pathlib import Path

from issue_agent.benchmark.runner import BenchmarkCase, BenchmarkRunner
from issue_agent.core.agent import Agent
from issue_agent.core.model import ModelResponse, ScriptedModelClient


OLD_TEST = """package demo;

import org.junit.Test;
import static org.junit.Assert.assertEquals;

public class ShippingServiceTest {
    private final ShippingService service = new ShippingService();

    @Test
    public void chargesFiveHundredForSmallOrder() {
        assertEquals(500, service.shippingFee(1000));
    }

    @Test
    public void chargesFiveHundredForEmptyOrder() {
        assertEquals(500, service.shippingFee(0));
    }
}
"""

NEW_TEST = OLD_TEST.replace("\n}\n", """

    @Test public void rejectsMinusOne() { assertNegative(-1); }
    @Test public void rejectsMinimumInteger() { assertNegative(Integer.MIN_VALUE); }
    @Test public void chargesAt9999() { assertEquals(500, service.shippingFee(9999)); }
    @Test public void isFreeAt10000() { assertEquals(0, service.shippingFee(10000)); }
    @Test public void isFreeAt10001() { assertEquals(0, service.shippingFee(10001)); }
    @Test public void isFreeAtMaximumInteger() { assertEquals(0, service.shippingFee(Integer.MAX_VALUE)); }

    private void assertNegative(int value) {
        try {
            service.shippingFee(value);
            org.junit.Assert.fail("Expected rejection");
        } catch (IllegalArgumentException expected) {
            assertEquals("Subtotal must not be negative", expected.getMessage());
        }
    }
}
""")

OLD_SOURCE = """package demo;

public class ShippingService {
    // 金额使用整数分，避免引入浮点数舍入问题。
    public int shippingFee(int subtotalCents) {
        return 500;
    }
}
"""

NEW_SOURCE = OLD_SOURCE.replace("        return 500;", """        if (subtotalCents < 0) {
            throw new IllegalArgumentException("Subtotal must not be negative");
        }
        return subtotalCents >= 10000 ? 0 : 500;""")


def tool(call_id, name, **arguments):
    return ModelResponse([{"type": "tool_use", "id": call_id, "name": name, "input": arguments}],
                         input_tokens=10, output_tokens=10, stop_reason="tool_use")


def run(root: Path) -> dict:
    source = root.parent / "gradle-test-repo"
    issue = source.joinpath("ISSUE.md").read_text(encoding="utf-8")

    def factory(workspace, task):
        # Git may check files out as CRLF on Windows. The real edit tool requires an exact
        # byte-for-byte match, so the deterministic smoke model mirrors the checkout style.
        actual_test = workspace.resolve("src/test/java/demo/ShippingServiceTest.java").read_bytes().decode("utf-8")
        actual_source = workspace.resolve("src/main/java/demo/ShippingService.java").read_bytes().decode("utf-8")
        newline = "\r\n" if "\r\n" in actual_source else "\n"
        desired_test = NEW_TEST.replace("\n", newline)
        desired_source = NEW_SOURCE.replace("\n", newline)
        model = ScriptedModelClient([
            tool("1", "edit_file", path="src/test/java/demo/ShippingServiceTest.java",
                 old_text=actual_test, new_text=desired_test),
            tool("2", "run_test"),
            tool("3", "edit_file", path="src/main/java/demo/ShippingService.java",
                 old_text=actual_source, new_text=desired_source),
            tool("4", "run_test"),
            ModelResponse([{"type": "text", "text": "Ready for independent verification."}], 10, 10),
            ModelResponse([{"type": "text", "text": json.dumps({
                "completed": True, "reason": "Required behavior and public regression tests are present.",
                "missing_requirements": []})}], 10, 10),
        ])
        return Agent(workspace, task, model, max_steps=10, test_timeout=600,
                     verification_model=model)

    case = BenchmarkCase("shipping-gradle-001", source,
        "3cf759e1ccbd444253a7e1dae176e8c08d6c6eec", issue,
        root / "benchmark_cases/shipping-gradle-001/hidden-tests", "src/test/java/demo")
    result = BenchmarkRunner(root / "benchmark_runs", factory, test_timeout=600).run(case)
    result["execution_mode"] = "scripted_model_smoke"
    result_path = root / "benchmark_runs" / result["run_id"] / "benchmark-result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    project = Path(__file__).resolve().parent
    outcome = run(project)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    raise SystemExit(0 if outcome["agent_completed"] and outcome["hidden_tests_passed"] else 2)
