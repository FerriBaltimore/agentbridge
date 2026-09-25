"""Pure route selection from declared support and observed capacity.

Callers must establish which provider quota windows apply to each model. This
module does not infer that relationship from a provider's pool name or label.
It selects before a turn; its decision must be persisted before execution.
"""

from dataclasses import dataclass
import math
import time

from ..errors import BridgeError
from ..models import identifier, model_id


def _nonnegative(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise BridgeError("invalid_request", f"{name} must be a finite nonnegative number.")
    return float(value)


@dataclass(frozen=True)
class QuotaObservation:
    """One quota window proven to apply to model_id, or every declared model.

    model_id=None means the caller has evidence that this window applies to
    every model declared by its RouteCandidate. It does not mean an unknown
    provider scope. An unproven scope must be omitted, leaving quota unknown.
    """

    used_percent: float | None
    observed_at: float | None
    model_id: str | None = None
    reset_at: float | None = None
    limit_reached: bool | None = None
    source: str | None = None
    window_id: str | None = None

    def __post_init__(self):
        if self.model_id is not None:
            model_id(self.model_id)
        if self.used_percent is not None:
            used = _nonnegative(self.used_percent, "used_percent")
            if used > 100:
                raise BridgeError("invalid_request", "used_percent must be at most 100.")
            object.__setattr__(self, "used_percent", used)
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _nonnegative(self.observed_at, "observed_at"))
        if self.reset_at is not None:
            object.__setattr__(self, "reset_at", _nonnegative(self.reset_at, "reset_at"))
        if self.limit_reached is not None and type(self.limit_reached) is not bool:
            raise BridgeError("invalid_request", "limit_reached must be a boolean or unknown.")
        for value in (self.source, self.window_id):
            if value is not None and (not isinstance(value, str) or not 0 < len(value) <= 512):
                raise BridgeError("invalid_request", "Quota evidence labels must be bounded strings.")


@dataclass(frozen=True)
class RouteCandidate:
    """One account with explicit model support and current scheduling facts."""

    account_id: str
    models: tuple[str, ...]
    quota: tuple[QuotaObservation, ...] = ()
    health: str = "unknown"
    cooldown_until: float | None = None
    in_flight: int = 0
    max_in_flight: int = 1
    assigned_turns: int = 0

    def __post_init__(self):
        identifier(self.account_id)
        models = tuple(self.models)
        for value in models:
            model_id(value)
        if len(set(models)) != len(models):
            raise BridgeError("invalid_request", "Declared models must be unique per account.")
        object.__setattr__(self, "models", models)
        quota = tuple(self.quota)
        if any(not isinstance(item, QuotaObservation) for item in quota):
            raise BridgeError("invalid_request", "quota must contain quota observations.")
        if any(item.model_id is not None and item.model_id not in models for item in quota):
            raise BridgeError("invalid_request", "A quota model must be declared for its account.")
        object.__setattr__(self, "quota", quota)
        if self.health not in {"healthy", "unknown", "unhealthy"}:
            raise BridgeError("invalid_request", "health must be healthy, unknown or unhealthy.")
        if self.cooldown_until is not None:
            object.__setattr__(self, "cooldown_until", _nonnegative(self.cooldown_until, "cooldown_until"))
        if (type(self.in_flight) is not int or self.in_flight < 0
                or type(self.max_in_flight) is not int or self.max_in_flight < 1):
            raise BridgeError("invalid_request", "In-flight counts must be nonnegative with positive capacity.")
        if type(self.assigned_turns) is not int or self.assigned_turns < 0:
            raise BridgeError("invalid_request", "assigned_turns must be a nonnegative observed count.")


@dataclass(frozen=True)
class RouteDecision:
    account_id: str
    model: str
    quota_state: str
    used_percent: float | None
    health: str
    in_flight: int
    reason: str
    affinity_break_reason: str | None = None
    affinity_break_evidence: dict | None = None


def _quota_state(candidate, model, now, quota_ttl):
    windows = [item for item in candidate.quota if item.model_id is None or item.model_id == model]
    if not windows:
        return "unknown", None
    fresh = [item for item in windows if item.observed_at is not None
             and item.observed_at <= now and now - item.observed_at < quota_ttl
             and (item.reset_at is None or item.reset_at > now)]
    if any(item.limit_reached is True or item.used_percent == 100 for item in fresh):
        return "exhausted", 100.0
    if len(fresh) != len(windows) or any(item.used_percent is None for item in fresh):
        return "unknown", None
    return "known", max(item.used_percent for item in fresh)


def _exhaustion_evidence(candidate, model, now, quota_ttl):
    def bounded_label(value):
        return (value if isinstance(value, str) and 0 < len(value) <= 256
                and all(32 <= ord(char) < 127 for char in value) else None)

    for item in candidate.quota:
        if item.model_id not in (None, model) or item.observed_at is None:
            continue
        if (item.observed_at > now or now - item.observed_at >= quota_ttl
                or item.reset_at is not None and item.reset_at <= now):
            continue
        if item.limit_reached is True or item.used_percent == 100:
            return {"account_id": candidate.account_id, "source": bounded_label(item.source),
                    "window_id": bounded_label(item.window_id), "observed_at": item.observed_at,
                    "reset_at": item.reset_at, "used_percent": item.used_percent,
                    "limit_reached": item.limit_reached}
    return None


def _unverified_exhaustion_reset(candidate, model, now, quota_ttl):
    """An expired observation cannot prove a rejected window has reopened."""
    resets = [item.reset_at for item in candidate.quota
              if item.model_id in (None, model) and item.observed_at is not None
              and item.observed_at <= now and now - item.observed_at >= quota_ttl
              and item.reset_at is not None and item.reset_at > now
              and (item.limit_reached is True or item.used_percent == 100)]
    return min(resets) if resets else None


def select_route(model, candidates, *, now=None, quota_ttl=60, affinity_account_id=None):
    """Keep an eligible affinity account, else choose the least-used candidate.

    Known, fresh quota beats unknown quota. Unknown remains eligible as a
    fallback and is never interpreted as zero usage. Assigned turn count is a
    fairness tie-breaker, not evidence of remaining provider quota. Selection
    itself does not reserve capacity; callers must persist and reserve atomically.
    """
    model_id(model)
    if affinity_account_id is not None:
        identifier(affinity_account_id)
    now = time.time() if now is None else _nonnegative(now, "now")
    quota_ttl = _nonnegative(quota_ttl, "quota_ttl")
    if quota_ttl == 0:
        raise BridgeError("invalid_request", "quota_ttl must be positive.")
    rows = tuple(candidates)
    if any(not isinstance(row, RouteCandidate) for row in rows):
        raise BridgeError("invalid_request", "candidates must contain route candidates.")
    if len({row.account_id for row in rows}) != len(rows):
        raise BridgeError("invalid_request", "An account may appear only once in route candidates.")
    supported = [row for row in rows if model in row.models]
    if not supported:
        raise BridgeError("model_unavailable", "No account declares support for this model.")
    affinity = next((row for row in supported if row.account_id == affinity_account_id), None)
    if affinity is not None:
        if affinity.health == "unhealthy":
            raise BridgeError("proxy_binding_unverified", "The affinity account could not be verified.")
        affinity_quota, affinity_used = _quota_state(affinity, model, now, quota_ttl)
        if affinity_quota != "exhausted":
            reset_at = _unverified_exhaustion_reset(affinity, model, now, quota_ttl)
            if reset_at is not None:
                raise BridgeError("quota_unknown", "The affinity account's prior quota rejection needs revalidation.",
                                  retryable=True,
                                  retry_after_ms=min(10000, math.ceil((reset_at - now) * 1000)))
            if affinity.cooldown_until is not None and affinity.cooldown_until > now:
                raise BridgeError("rate_limited", "The affinity account is temporarily rate limited.",
                                  retryable=True,
                                  retry_after_ms=math.ceil((affinity.cooldown_until - now) * 1000))
            if affinity.in_flight >= affinity.max_in_flight:
                raise BridgeError("account_busy", "The affinity account has an active turn.",
                                  retryable=True)
            return RouteDecision(affinity.account_id, model, affinity_quota, affinity_used,
                                 affinity.health, affinity.in_flight, "affinity")
    break_evidence = (_exhaustion_evidence(affinity, model, now, quota_ttl)
                      if affinity is not None else None)
    eligible = []
    excluded = {"unhealthy": 0, "cooldown": 0, "busy": 0,
                "quota_exhausted": 0, "quota_unknown": 0}
    for row in supported:
        if row.health == "unhealthy":
            excluded["unhealthy"] += 1
            continue
        quota_state, used = _quota_state(row, model, now, quota_ttl)
        if quota_state == "exhausted":
            excluded["quota_exhausted"] += 1
            continue
        if _unverified_exhaustion_reset(row, model, now, quota_ttl) is not None:
            excluded["quota_unknown"] += 1
            continue
        if row.cooldown_until is not None and row.cooldown_until > now:
            excluded["cooldown"] += 1
            continue
        if row.in_flight >= row.max_in_flight:
            excluded["busy"] += 1
            continue
        key = (row.health != "healthy", quota_state != "known",
               used if used is not None else 101,
               row.in_flight / row.max_in_flight, row.assigned_turns,
               row.account_id)
        eligible.append((key, row, quota_state, used))
    if not eligible:
        reason = ("quota_exhausted" if excluded["quota_exhausted"] == len(supported) else
                  "quota_unknown" if excluded["quota_unknown"] + excluded["quota_exhausted"] == len(supported) else
                  "account_busy" if excluded["busy"] == len(supported) else "provider_unavailable")
        raise BridgeError(reason, "No account is currently available for this model.",
                          details={"excluded": excluded})
    _, selected, quota_state, used = min(eligible, key=lambda item: item[0])
    return RouteDecision(selected.account_id, model, quota_state, used, selected.health,
                         selected.in_flight, "least_used" if quota_state == "known" else "quota_unknown",
                         "quota_exhausted" if break_evidence is not None else None,
                         break_evidence)
