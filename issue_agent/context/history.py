"""DeepSeek-only projection of successful old writes; original history is untouched."""
import copy
import json
from collections import Counter
from pathlib import PurePosixPath

#判断一个 history group 能不能安全处理
def _pairs(group: list[dict]) -> list[tuple[dict, dict]] | None:
    if len(group) != 2 or group[0].get("role") != "assistant" or group[1].get("role") != "user":
        return None
    calls, results = group[0].get("content"), group[1].get("content")
    if not isinstance(calls, list) or not isinstance(results, list):
        return None
    if any(not isinstance(b, dict) for b in calls + results):
        return None
    # The probe covered thinking followed by optional text and multiple client tools.
    if not calls or calls[0].get("type") != "thinking":
        return None
    if not isinstance(calls[0].get("thinking"), str) or not isinstance(calls[0].get("signature"), str) or not calls[0]["signature"]:
        return None
    seen_tool = False
    for b in calls[1:]:
        if b.get("type") == "tool_use":
            seen_tool = True
        elif b.get("type") != "text" or seen_tool:
            return None
    tools = [b for b in calls if b.get("type") == "tool_use"]
    if len(tools) < 2 or any(b.get("type") != "tool_result" for b in results):
        return None
    ids = [b.get("id") for b in tools]
    result_ids = [b.get("tool_use_id") for b in results]
    if any(not isinstance(cid, str) or not cid for cid in ids + result_ids):
        return None
    if len(set(ids)) != len(ids) or len(set(result_ids)) != len(result_ids) or set(ids) != set(result_ids):
        return None
    mapped = {b["tool_use_id"]: b for b in results}
    return [(b, mapped[b["id"]]) for b in tools]


#判断某一个 tool call 能不能变成“历史事实” 只认可两个工具：edit_file、create_file
def _fact(call: dict, result: dict, step: int | None) -> dict | None:
    name, args = call.get("name"), call.get("input")
    if name not in ("edit_file", "create_file") or not isinstance(args, dict):
        return None
    expected = {"path", "old_text", "new_text"} if name == "edit_file" else {"path", "content"}
    if set(args) != expected or any(not isinstance(v, str) for v in args.values()):
        return None
    value = args["path"].replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ":" in value or ".." in path.parts or path.as_posix() == ".":
        return None
    try:
        payload = json.loads(result["content"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True or result.get("is_error") is not False:
        return None
    if payload.get("result") != "Modified " + path.as_posix():
        return None
    return {"tool_call_id": call["id"], "step": step, "tool": name,
            "path": path.as_posix(), "outcome": "succeeded"}


def compact_history(groups: list[list[dict]], *, enabled: bool = False,
                    call_steps: dict[str, int] | None = None) -> list[list[dict]]:
    if not enabled:
        return groups
    ids = Counter(b.get("id") for g in groups for m in g
                  if isinstance(m.get("content"), list) for b in m["content"]
                  if isinstance(b, dict) and b.get("type") == "tool_use")
    result_ids = Counter(b.get("tool_use_id") for g in groups for m in g
                         if isinstance(m.get("content"), list) for b in m["content"]
                         if isinstance(b, dict) and b.get("type") == "tool_result")
    projected = []
    for index, group in enumerate(groups):
        pairs = _pairs(group) if index < len(groups) - 2 else None
        if not pairs or any(ids[c["id"]] != 1 or result_ids[c["id"]] != 1 for c, _ in pairs):
            projected.append(group)
            continue
        facts = [f for c, r in pairs if (f := _fact(c, r, (call_steps or {}).get(c["id"]))) is not None]
        # Keep at least one original tool pair: do not replace an empty message with facts.
        if not facts or len(facts) == len(pairs):
            projected.append(group)
            continue
        removed = {f["tool_call_id"] for f in facts}
        new_group = copy.deepcopy(group)
        new_group[0]["content"] = [b for b in new_group[0]["content"]
                                  if not (b.get("type") == "tool_use" and b.get("id") in removed)]
        new_group[1]["content"] = [b for b in new_group[1]["content"] if b.get("tool_use_id") not in removed]
        # Facts come after all retained results, without interposing a message.
        new_group[1]["content"].append({"type": "text", "text": json.dumps(
            {"historical_facts": {"schema_version": 1, "facts": facts},
             "scope": "Historical tool execution only; not current file content or requirement completion."},
            ensure_ascii=False)})
        projected.append(new_group)
    return projected
