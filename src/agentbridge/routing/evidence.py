"""Validate bounded policy evidence before writing a route event."""

from dataclasses import asdict
import math

from ..errors import BridgeError
from ..models import identifier
from .selector import RouteDecision


_BREAK_FIELDS = frozenset({
    "account_id", "source", "window_id", "observed_at", "reset_at",
    "used_percent", "limit_reached",
})


def _bounded_label(value):
    return (value is None or (isinstance(value, str) and 0 < len(value) <= 256
            and all(32 <= ord(char) < 127 for char in value)))


def _optional_finite(value):
    return value is None or (not isinstance(value, bool)
                             and isinstance(value, (int, float))
                             and math.isfinite(value) and value >= 0)


def _break_evidence(values):
    reason = values.get("affinity_break_reason")
    evidence = values.get("affinity_break_evidence")
    if reason is None and evidence is None:
        return {}
    if (reason != "quota_exhausted" or values.get("reason") == "affinity"
            or not isinstance(evidence, dict) or evidence.keys() != _BREAK_FIELDS):
        raise BridgeError("invalid_request", "The affinity break evidence is invalid.")
    identifier(evidence["account_id"])
    if (not _bounded_label(evidence["source"])
            or not _bounded_label(evidence["window_id"])
            or not _optional_finite(evidence["observed_at"])
            or evidence["observed_at"] is None
            or not _optional_finite(evidence["reset_at"])
            or not _optional_finite(evidence["used_percent"])
            or (evidence["used_percent"] is not None and evidence["used_percent"] > 100)
            or (evidence["limit_reached"] is not None
                and type(evidence["limit_reached"]) is not bool)
            or (evidence["used_percent"] != 100 and evidence["limit_reached"] is not True)):
        raise BridgeError("invalid_request", "The affinity break evidence is invalid.")
    return {"affinity_break_reason": reason, "affinity_break_evidence": evidence}


def route_evidence(decision, account_id, model):
    if isinstance(decision, RouteDecision):
        values = asdict(decision)
    elif isinstance(decision, dict):
        values = decision
    else:
        raise BridgeError("invalid_request", "Automatic routing requires a route decision.")
    if values.get("account_id") != account_id or values.get("model") != model:
        raise BridgeError("invalid_request", "The route decision must match the admitted account and model.")
    state = values.get("quota_state")
    reason = values.get("reason")
    used = values.get("used_percent")
    if state not in {"known", "unknown"} or reason not in {"least_used", "quota_unknown", "affinity"}:
        raise BridgeError("invalid_request", "The route decision has an unsupported policy result.")
    if ((reason == "least_used" and state != "known")
            or (reason == "quota_unknown" and state != "unknown")):
        raise BridgeError("invalid_request", "The route decision's reason and quota state disagree.")
    if ((state == "known" and (isinstance(used, bool) or not isinstance(used, (int, float))
                                or not math.isfinite(used) or not 0 <= used < 100))
            or (state == "unknown" and used is not None)):
        raise BridgeError("invalid_request", "The route quota observation is inconsistent.")
    return {"account_id": account_id, "model": model, "quota_state": state,
            "used_percent": used, "reason": reason, **_break_evidence(values)}
