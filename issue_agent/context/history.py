"""DeepSeek-only projection of successful old edits; original history is untouched."""
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
    if not tools or any(b.get("type") != "tool_result" for b in results):
        return None
    ids = [b.get("id") for b in tools]
    result_ids = [b.get("tool_use_id") for b in results]
    if any(not isinstance(cid, str) or not cid for cid in ids + result_ids):
        return None
    if len(set(ids)) != len(ids) or len(set(result_ids)) != len(result_ids) or set(ids) != set(result_ids):
        return None
    mapped = {b["tool_use_id"]: b for b in results}
    return [(b, mapped[b["id"]]) for b in tools]


# 只归档成功 edit_file，不根据操作成功推断需求完成。
def _fact(call: dict, result: dict, step: int | None) -> dict | None:
    name, args = call.get("name"), call.get("input")
    if name != "edit_file" or not isinstance(args, dict):
        return None
    expected = {"path", "old_text", "new_text"}
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


def _summary(facts: list[dict]) -> list[dict]:
    return [{"role": "user", "content": json.dumps(
        {"historical_facts": {"schema_version": 1, "facts": facts},
         "scope": "Historical tool execution only; not current file content or requirement completion."},
        ensure_ascii=False)}]


def compact_history(groups: list[list[dict]], *, enabled: bool = False,
                    call_steps: dict[str, int] | None = None,
                    decisions: list[dict] | None = None) -> list[list[dict]]:
    """Project old edits; optional decisions are local diagnostics, never Trace events."""
    if not enabled:
        return groups
    blocks = [b for g in groups for m in g if isinstance(m.get("content"), list)
              for b in m["content"] if isinstance(b, dict)]
    ids = Counter(b["id"] for b in blocks if b.get("type") == "tool_use" and isinstance(b.get("id"), str))
    result_ids = Counter(b["tool_use_id"] for b in blocks if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str))
    projected, archived = [], []
    for index, group in enumerate(groups):
        pairs = _pairs(group)
        edits = [b for m in group if isinstance(m.get("content"), list)
                 for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ("edit_file", "create_file")]
        results = [b for m in group if isinstance(m.get("content"), list)
                   for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
        facts = []
        records = []
        for call in edits:
            cid = call.get("id")
            matches = [r for r in results if r.get("tool_use_id") == cid]
            succeeded = False
            if len(matches) == 1:
                try:
                    payload = json.loads(matches[0]["content"])
                    succeeded = isinstance(payload, dict) and payload.get("ok") is True and matches[0].get("is_error") is False
                except (KeyError, TypeError, ValueError):
                    pass
            record = {"tool_call_id": cid, "candidate": succeeded and call.get("name") == "edit_file", "compressed": False, "skip_reason": None}
            if call.get("name") != "edit_file":
                record["skip_reason"] = "unsupported_create_file"
            elif index >= len(groups) - 2:
                record["skip_reason"] = "recent_two_groups"
            elif not pairs:
                record["skip_reason"] = "unverified_shape_or_invalid_pairing"
            elif not isinstance(cid, str) or ids[cid] != 1 or result_ids[cid] != 1:
                record["skip_reason"] = "non_unique_id"
            elif not succeeded:
                record["skip_reason"] = "not_proven_success"
            else:
                fact = _fact(call, matches[0], (call_steps or {}).get(cid))
                if fact is None:
                    record["skip_reason"] = "invalid_arguments_path_or_success_output"
                else:
                    facts.append(fact)
            records.append(record)
        # Any globally ambiguous pair makes this group unsafe to rewrite.
        if facts and any(ids[c["id"]] != 1 or result_ids[c["id"]] != 1 for c, _ in pairs):
            facts = []
            for record in records:
                if record["skip_reason"] is None: record["skip_reason"] = "non_unique_id"
        single = bool(pairs and len(pairs) == 1)
        if facts and not single and len(facts) == len(pairs):
            facts = []
            for record in records:
                if record["skip_reason"] is None: record["skip_reason"] = "empty_mixed_result"
        if facts:
            removed = {f["tool_call_id"] for f in facts}
            for record in records:
                if record["tool_call_id"] in removed:
                    record["compressed"] = True
            archived.extend(facts)
            if not single:
                new_group = copy.deepcopy(group)
                new_group[0]["content"] = [b for b in new_group[0]["content"]
                    if not (b.get("type") == "tool_use" and b.get("id") in removed)]
                new_group[1]["content"] = [b for b in new_group[1]["content"] if b.get("tool_use_id") not in removed]
                projected.append(new_group)
            # Single edit: omit both messages, including associated thinking/text.
        else:
            projected.append(group)
        if decisions is not None:
            decisions.extend(records)
    # One ordinary summary after retained history: never interpose facts in a tool pair.
    if archived:
        projected.append(_summary(archived))
    return projected
