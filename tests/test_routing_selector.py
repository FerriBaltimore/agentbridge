"""Routing policy is deterministic and never invents missing capacity."""

import pytest

from agentbridge.errors import BridgeError
from agentbridge.routing import QuotaObservation, RouteCandidate, select_route


NOW = 1_000.0
MODEL = "claude-sonnet"


def candidate(account_id, used=None, *, models=(MODEL,), observed_at=NOW,
              health="healthy", in_flight=0, max_in_flight=1, cooldown_until=None,
              limit_reached=None, reset_at=None, assigned_turns=0):
    quota = () if used is None else (QuotaObservation(used, observed_at, MODEL,
                                                     reset_at, limit_reached),)
    return RouteCandidate(account_id, models, quota, health, cooldown_until,
                          in_flight, max_in_flight, assigned_turns)


def test_selects_smallest_fraction_of_quota_used_not_absolute_spend():
    rows = [candidate("small", 50), candidate("large", 20)]
    decision = select_route(MODEL, rows, now=NOW)
    assert decision.account_id == "large"
    assert decision.used_percent == 20
    assert decision.quota_state == "known"


def test_exact_declared_model_support_required():
    rows = [candidate("other", models=("claude-sonnet-latest",)),
            candidate("matching", 90)]
    assert select_route(MODEL, rows, now=NOW).account_id == "matching"
    with pytest.raises(BridgeError) as caught:
        select_route("grok", rows, now=NOW)
    assert caught.value.code == "model_unavailable"


def test_unknown_and_stale_quota_are_fallback_not_zero_usage():
    rows = [candidate("missing"), candidate("stale", 0, observed_at=NOW - 61),
            candidate("observed", 90)]
    assert select_route(MODEL, rows, now=NOW).account_id == "observed"
    decision = select_route(MODEL, rows[:2], now=NOW)
    assert decision.quota_state == "unknown"
    assert decision.used_percent is None
    assert decision.reason == "quota_unknown"


def test_future_observation_and_passed_reset_are_unknown():
    rows = [candidate("future", 0, observed_at=NOW + 1),
            candidate("reset", 0, reset_at=NOW), candidate("known", 80)]
    assert select_route(MODEL, rows, now=NOW).account_id == "known"


def test_quota_windows_use_limiting_window_and_scope_to_model():
    other_model = QuotaObservation(99, NOW, "grok")
    account_wide = QuotaObservation(70, NOW)
    model_window = QuotaObservation(30, NOW, MODEL)
    rows = [RouteCandidate("bounded", (MODEL, "grok"),
                           (other_model, account_wide, model_window), "healthy"),
            candidate("less_used", 60)]
    assert select_route(MODEL, rows, now=NOW).account_id == "less_used"
    assert select_route("grok", rows, now=NOW).used_percent == 99


def test_unknown_applicable_window_prevents_claiming_known_capacity():
    rows = [RouteCandidate("uncertain", (MODEL,),
                           (QuotaObservation(1, NOW), QuotaObservation(None, NOW, MODEL)),
                           "healthy"), candidate("known", 90)]
    assert select_route(MODEL, rows, now=NOW).account_id == "known"


def test_fresh_exhaustion_overrides_another_stale_window():
    rows = [RouteCandidate("mixed", (MODEL,),
                           (QuotaObservation(1, NOW - 61), QuotaObservation(100, NOW)),
                           "healthy"), candidate("ready", 50)]
    assert select_route(MODEL, rows, now=NOW).account_id == "ready"


def test_exhaustion_and_cooldown_do_not_trigger_hidden_failover():
    rows = [candidate("exhausted", 100), candidate("limited", 20, limit_reached=True),
            candidate("cooling", 1, cooldown_until=NOW + 10), candidate("ready", 60)]
    assert select_route(MODEL, rows, now=NOW).account_id == "ready"
    with pytest.raises(BridgeError) as caught:
        select_route(MODEL, rows[:2], now=NOW)
    assert caught.value.code == "quota_exhausted"


def test_busy_and_unhealthy_accounts_excluded():
    rows = [candidate("busy", 1, in_flight=1),
            candidate("unhealthy", 1, health="unhealthy"), candidate("ready", 60)]
    assert select_route(MODEL, rows, now=NOW).account_id == "ready"
    with pytest.raises(BridgeError) as caught:
        select_route(MODEL, rows[:1], now=NOW)
    assert caught.value.code == "account_busy"


def test_deterministic_ties_and_in_flight_load():
    rows = [candidate("b", 25, in_flight=1, max_in_flight=3),
            candidate("a", 25, in_flight=1, max_in_flight=3)]
    assert select_route(MODEL, rows, now=NOW).account_id == "a"
    rows.append(candidate("c", 25))
    assert select_route(MODEL, list(reversed(rows)), now=NOW).account_id == "c"


def test_observed_assigned_turns_spread_equal_quota_and_unknown_fallback():
    known = [candidate("a", 25, assigned_turns=7),
             candidate("b", 25, assigned_turns=2)]
    assert select_route(MODEL, known, now=NOW).account_id == "b"
    unknown = [candidate("a", assigned_turns=7),
               candidate("b", assigned_turns=2)]
    decision = select_route(MODEL, unknown, now=NOW)
    assert decision.account_id == "b"
    assert decision.quota_state == "unknown"
    assert decision.used_percent is None


def test_fresh_quota_usage_outweighs_assigned_turn_count():
    rows = [candidate("a", 80, assigned_turns=0),
            candidate("b", 20, assigned_turns=100)]
    assert select_route(MODEL, rows, now=NOW).account_id == "b"


def test_health_unknown_is_fallback():
    rows = [candidate("health_unknown", 1, health="unknown"), candidate("healthy", 90)]
    assert select_route(MODEL, rows, now=NOW).account_id == "healthy"


def test_rejects_duplicate_account_candidates():
    with pytest.raises(BridgeError):
        select_route(MODEL, [candidate("same", 10), candidate("same", 20)], now=NOW)


def test_rejects_invalid_quota_values():
    with pytest.raises(BridgeError):
        QuotaObservation(101, NOW)
    with pytest.raises(BridgeError):
        QuotaObservation(False, NOW)
    with pytest.raises(BridgeError):
        RouteCandidate("account", (MODEL,), (QuotaObservation(50, NOW, "grok"),))
    with pytest.raises(BridgeError):
        candidate("account", assigned_turns=-1)
