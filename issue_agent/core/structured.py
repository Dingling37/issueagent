import json
from .model import ModelResponse


def json_object(response: ModelResponse) -> dict:
    if response.tool_calls or response.stop_reason != "end_turn":
        raise ValueError("Verifier must return a complete JSON response without tools")
    text = "\n".join(block.get("text", "") for block in response.content if block.get("type") == "text").strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    elif text.startswith("```\n") and text.endswith("```"):
        text = text[4:-3].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value
