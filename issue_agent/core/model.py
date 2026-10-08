# 将教学代码中直接调用 client.messages.create 的部分独立出来。
import os
from dataclasses import dataclass, field
from typing import Any, Protocol
from ..config import ModelConfig, PROVIDERS
from ..security import protect_secret, redact


@dataclass
# 统一的响应对象：Agent 不需要直接处理 Anthropic SDK 的响应类。
class ModelResponse:
    content: list[dict[str, Any]]
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"
    raw_usage: dict[str, Any] | None = None
    request_id: str | None = None
    model_id: str | None = None

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        # 一次响应可以同时包含文字块和工具调用块，这里只取 tool_use。
        return [block for block in self.content if block.get("type") == "tool_use"]


# Protocol 表达接口约定：只要有符合签名的 call 方法，就能交给 Agent 使用。
class ModelClient(Protocol):
    def call(self, context: list[dict], tools: list[dict], *, max_tokens: int = 4096,
             tool_choice: dict | None = None, thinking: dict | None = None) -> ModelResponse: ...


class ModelRequestError(RuntimeError):
    """Safe provider metadata for Runtime; never expose SDK exception bodies."""
    def __init__(self, error):
        from .recovery import FailureClassifier
        super().__init__(redact(f"Model request failed: {error}"))
        self.failure_kind = FailureClassifier.classify(error)
        self.status_code = getattr(error, "status_code", None)
        self.request_id = getattr(error, "request_id", None)
        headers = getattr(getattr(error, "response", None), "headers", {}) or getattr(error, "headers", {})
        retry_after = headers.get("retry-after", headers.get("Retry-After"))
        self.headers = {"retry-after": retry_after} if retry_after is not None else {}


class AnthropicModelClient:
    def __init__(self, model: str | None = None, *, config: ModelConfig | None = None) -> None:
        # 延迟导入：离线测试使用 ScriptedModelClient，不必安装 SDK 或提供密钥。
        from anthropic import Anthropic
        self.model = model or (config.model if config else os.environ.get("MODEL_ID"))
        self.provider = config.provider if config else "anthropic"
        if not self.model:
            raise ValueError("Set MODEL_ID or pass --model")
        key = config.api_key if config else os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("Set ANTHROPIC_API_KEY in the process environment")
        protect_secret(key)
        self.client = Anthropic(api_key=key, base_url=PROVIDERS[config.provider] if config else os.environ.get("ANTHROPIC_BASE_URL"),
                                timeout=120.0, max_retries=0)

    def call(self, context: list[dict], tools: list[dict], *, max_tokens: int = 4096,
             tool_choice: dict | None = None, thinking: dict | None = None) -> ModelResponse:
        from ..prompts.issue_agent import SYSTEM
        # Omit the option for executor calls, preserving the existing provider defaults.
        options = {"tool_choice": tool_choice} if tool_choice is not None else {}
        if thinking is not None:
            options["thinking"] = thinking
        try:
            response = self.client.messages.create(model=self.model, system=SYSTEM,
                                                   messages=context, tools=tools, max_tokens=max_tokens, **options)
        except Exception as exc:
            raise ModelRequestError(exc) from None
        # SDK 对象转为普通字典，便于写 JSON 日志和保存到 AgentState.messages。
        return ModelResponse([block.model_dump(exclude_none=True) for block in response.content],
                             response.usage.input_tokens, response.usage.output_tokens,
                             response.stop_reason or "unknown",
                             response.usage.model_dump(exclude_unset=True) if hasattr(response.usage, "model_dump")
                             else dict(vars(response.usage)),
                             getattr(response, "_request_id", None),
                             getattr(response, "model", self.model))


def create_model_client(config: ModelConfig) -> ModelClient:
    # Future providers with different protocols get their own adapter here.
    return AnthropicModelClient(config=config)


@dataclass
# 测试替身：按顺序返回预先准备的响应，不调用真实模型，也不消耗 token 费用。
class ScriptedModelClient:
    """Deterministic test double; never used implicitly by the real CLI."""
    responses: list[ModelResponse]
    contexts: list[list[dict]] = field(default_factory=list)
    token_limits: list[int] = field(default_factory=list)
    tool_choices: list[dict | None] = field(default_factory=list)
    tool_schemas: list[list[dict]] = field(default_factory=list)
    thinking_options: list[dict | None] = field(default_factory=list)

    def call(self, context: list[dict], tools: list[dict], *, max_tokens: int = 4096,
             tool_choice: dict | None = None, thinking: dict | None = None) -> ModelResponse:
        self.contexts.append(context)
        self.token_limits.append(max_tokens)
        self.tool_choices.append(tool_choice)
        self.thinking_options.append(thinking)
        self.tool_schemas.append(tools)
        if not self.responses:
            raise RuntimeError("Scripted model exhausted")
        return self.responses.pop(0)
