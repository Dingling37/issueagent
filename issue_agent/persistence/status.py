"""Persisted lifecycle; legacy Agent result names remain compatible."""
from enum import Enum

class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    PAUSED_USER = "PAUSED_USER"
    PAUSED_AUTH = "PAUSED_AUTH"
    PAUSED_QUOTA = "PAUSED_QUOTA"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    RECOVERING = "RECOVERING"
    VERIFYING = "VERIFYING"
    DELIVERING = "DELIVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
PAUSED = {s.value for s in RunStatus if s.value.startswith("PAUSED_")}
ACTIVE = {"RUNNING", "VERIFYING", "DELIVERING", "RECOVERING"}

def canonical_status(status):
    status = status.upper()
    if status in {"VERIFICATION_ERROR", "VERIFICATION_UNAVAILABLE"}:
        return "FAILED"
    if status == "PENDING":
        return "RUNNING"
    return RunStatus(status).value

def validate_transition(previous, following):
    from ..core.recovery import StateConflict
    previous, following = canonical_status(previous), canonical_status(following)
    if previous == following:
        return
    if previous in TERMINAL or (previous in PAUSED and following not in {"RUNNING", "CANCELLED", "FAILED"}):
        raise StateConflict(f"Invalid Run transition: {previous} -> {following}")
