from dataclasses import dataclass
from typing import Protocol
from ..core.state import AgentState


# 新增的完成判定入口：Agent 自己说完成，不等于任务经过验证。
@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    feedback: str


# 接口约定：验证器读任务状态，返回是否通过和反馈原因。
class Verifier(Protocol):
    def verify(self, state: AgentState) -> VerificationResult: ...


# 注意：这只是 Phase 2 的占位实现，固定拒绝，不执行 Java 测试或 LLM 审查。
# Agent 遇到它会以 verification_unavailable 停止，避免浪费调用或误报完成。
class PendingVerifier:
    def verify(self, state: AgentState) -> VerificationResult:
        return VerificationResult(False, "Verification unavailable in Phase 2: Maven/Gradle tests, "
                                  "actual Git diff checks and LLM requirement review must be connected "
                                  "in Phase 3. The task cannot be marked completed.")
