"""Central runtime recovery; adapters never retry transport failures."""
from dataclasses import dataclass, asdict
from math import isfinite
from enum import Enum
from time import monotonic, sleep
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from ..persistence import PersistenceError

class FailureKind(str, Enum):
    TRANSIENT = "network_timeout_or_5xx"
    RATE_LIMIT = "rate_limit"
    AUTH = "authentication"
    QUOTA = "quota"
    BUDGET = "budget"
    DATABASE = "database"
    CONFLICT = "state_conflict"
    FATAL = "fatal"

class RecoveryError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind

class StateConflict(PersistenceError):
    pass

class FailureClassifier:
    @staticmethod
    def classify(error):
        if getattr(error, "failure_kind", None) is not None:
            return error.failure_kind
        if isinstance(error, RecoveryError):
            return error.kind
        if isinstance(error, StateConflict):
            return FailureKind.CONFLICT
        from sqlalchemy.exc import SQLAlchemyError
        if isinstance(error, (PersistenceError, SQLAlchemyError)):
            return FailureKind.DATABASE
        status = getattr(error, "status_code", None)
        body = (str(getattr(error, "body", "")) + " " + str(error)).lower()
        if status == 401:
            return FailureKind.AUTH
        if any(x in body for x in ("insufficient_quota", "insufficient balance", "insufficient_balance",
                "credit balance", "quota exceeded", "quota_exceeded", "balance insufficient", "billing_hard_limit",
                "insufficient credits", "insufficient funds")):
            return FailureKind.QUOTA
        if status == 429:
            return FailureKind.RATE_LIMIT
        if status is not None and 500 <= status < 600:
            return FailureKind.TRANSIENT
        if isinstance(error, (TimeoutError, ConnectionError)) or type(error).__name__ in {
                "APITimeoutError", "APIConnectionError", "ConnectTimeout", "ReadTimeout", "ConnectError",
                "WriteTimeout", "PoolTimeout", "ReadError", "WriteError", "RemoteProtocolError"}:
            return FailureKind.TRANSIENT
        return FailureKind.FATAL

@dataclass(frozen=True)
class RecoveryPolicy:
    max_retries: int = 2
    base_delay: float = 1.0
    max_delay: float = 30.0

    def __post_init__(self):
        if type(self.max_retries) is not int or self.max_retries < 0 or any(
                not isfinite(v) or v < 0 for v in (self.base_delay, self.max_delay)):
            raise ValueError("Invalid recovery limits")

    def delay(self, error, attempt):
        if FailureClassifier.classify(error) == FailureKind.RATE_LIMIT:
            headers = getattr(getattr(error, "response", None), "headers", None) or getattr(error, "headers", {})
            value = headers.get("retry-after", headers.get("Retry-After"))
            if value is not None:
                try:
                    delay = float(value)
                    if not isfinite(delay):
                        raise ValueError("Non-finite Retry-After")
                    return max(0.0, delay)
                except (ValueError, TypeError):
                    try:
                        return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
        return min(self.max_delay, self.base_delay * 2 ** attempt)

@dataclass(frozen=True)
class BudgetLimits:
    llm_calls: int | None = None
    tool_calls: int | None = None
    test_runs: int | None = None
    total_tokens: int | None = None
    runtime_seconds: float | None = None

    def __post_init__(self):
        if any(v is not None and (not isfinite(v) or v < 0) for v in asdict(self).values()):
            raise ValueError("Budget limits must be nonnegative")

class BudgetGuard:
    def __init__(self, state, limits=None, clock=monotonic):
        self.state, self.limits, self.clock = state, limits or BudgetLimits(), clock
        self.started, self.elapsed = clock(), state.runtime_seconds

    def update_time(self):
        self.state.runtime_seconds = self.elapsed + self.clock() - self.started

    def check_runtime(self):
        self.update_time()
        limit = self.limits.runtime_seconds
        if limit is not None and self.state.runtime_seconds >= limit:
            raise RecoveryError(FailureKind.BUDGET, "Budget reached: runtime_seconds")

    def check(self, resource=None, amount=1):
        self.update_time()
        values = {"llm_calls": self.state.budget_usage.get("llm_calls", self.state.llm_calls), "tool_calls": self.state.tool_calls,
                  "test_runs": self.state.test_runs, "total_tokens": sum(self.state.token_usage.values()),
                  "runtime_seconds": self.state.runtime_seconds}
        for name, used in values.items():
            limit = getattr(self.limits, name)
            if limit is not None and (used >= limit or (name == resource and used + amount > limit)):
                raise RecoveryError(FailureKind.BUDGET, f"Budget reached: {name}")

    def consume(self, resource):
        self.check(resource)
        self.state.budget_usage[resource] = self.state.budget_usage.get(resource, getattr(self.state, resource)) + 1
        setattr(self.state, resource, getattr(self.state, resource) + 1)

    def wait(self, seconds, sleeper=sleep):
        while seconds > 0:
            self.check()
            duration = min(seconds, 0.1)
            sleeper(duration)
            seconds -= duration
        self.check()
