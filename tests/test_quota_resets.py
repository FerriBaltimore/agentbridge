"""Reset redemption never discovers real accounts or calls a real provider."""
from dataclasses import replace
import json
import threading
import time

import pytest

from agentbridge.errors import BridgeError
from agentbridge import Bridge
from agentbridge.models import Account
from agentbridge import quota_resets
from agentbridge.store import Store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    account = Account("test-account", "codex", home=str(tmp_path / "home"), email="owner@example.test")
    store.account(account)
    state = {"calls": [], "outcome": "reset", "closed": 0, "email": account.email}

    class Probe:
        def __init__(self, selected):
            assert selected == account

        def _rpc(self, method, params=None):
            state["calls"].append((method, params))
            if method == "account/read":
                return {"account": {"type": "chatgpt", "email": state["email"]}}
            if method == "account/rateLimitResetCredit/consume":
                with store.connect() as db:
                    row = db.execute("SELECT * FROM quota_reset_requests WHERE request_key=?",
                                     (params["idempotencyKey"],)).fetchone()
                assert row["state"] == "submitted"
                assert json.loads(row["identity"])["email"] == account.email
                if "enter" in state:
                    state["enter"].set()
                    assert state["release"].wait(5)
                if "error" in state:
                    raise state["error"]
                return {"outcome": state["outcome"], "private": "secret-provider-payload"}
            return {}

        def _notify(self, method, params):
            state["calls"].append((method, params))

        def close(self):
            state["closed"] += 1

    monkeypatch.setattr(quota_resets, "CodexAppServerProbe", Probe)
    return store, account, state


@pytest.mark.parametrize("native,normalized", [
    ("reset", "reset"), ("alreadyRedeemed", "already_redeemed"),
    ("nothingToReset", "nothing_to_reset"), ("noCredit", "no_credit"),
])
def test_receipt_survives_restart_without_second_provider_call(setup, native, normalized):
    store, account, state = setup
    state["outcome"] = native
    result = quota_resets.consume(store, account, "reset-request", "chosen-credit")
    assert result["outcome"] == normalized
    assert result["replayed"] is False
    assert result["state"] == "completed"
    assert result["observed_at"].endswith("+00:00")
    calls = list(state["calls"])
    saved = quota_resets.consume(Store(store.root), account, "reset-request", "chosen-credit")
    assert saved == {**result, "replayed": True}
    assert state["calls"] == calls
    assert state["closed"] == 1
    assert "secret-provider-payload" not in store.path.read_bytes().decode("utf-8", "ignore")


def test_timeout_reconciles_original_key_and_blocks_another_reset(setup):
    store, account, state = setup
    state["error"] = BridgeError("provider_timeout", "Do not persist secret-provider-error.")
    with pytest.raises(BridgeError) as caught:
        quota_resets.consume(store, account, "same-key")
    assert caught.value.code == "unknown_outcome"
    assert caught.value.safe_data()["outcome"] == "unknown"
    assert caught.value.safe_data()["retryable"] is False
    assert caught.value.safe_data()["details"]["request_key"] == "same-key"
    with pytest.raises(BridgeError, match="Reconcile") as pending:
        quota_resets.consume(Store(store.root), account, "different-key")
    assert pending.value.code == "reset_pending"
    del state["error"]
    state["outcome"] = "alreadyRedeemed"
    result = quota_resets.consume(Store(store.root), account, "same-key")
    assert result["outcome"] == "already_redeemed" and result["replayed"]
    consumed = [params for method, params in state["calls"] if method.endswith("/consume")]
    assert consumed == [{"idempotencyKey": "same-key"}] * 2
    assert "secret-provider-error" not in store.path.read_bytes().decode("utf-8", "ignore")


def test_changed_credit_or_account_rejects_same_key(setup):
    store, account, state = setup
    quota_resets.consume(store, account, "same-key", "credit-1")
    for selected, credit in ((account, "credit-2"), (replace(account, id="another", home=account.home + "-2"), "credit-1")):
        store.account(selected)
        with pytest.raises(BridgeError) as caught:
            quota_resets.consume(store, selected, "same-key", credit)
        assert caught.value.code == "idempotency_conflict"
    assert len([method for method, _ in state["calls"] if method.endswith("/consume")]) == 1


def test_changed_principal_does_not_redeem(setup):
    store, account, state = setup
    state["email"] = "someone-else@example.test"
    with pytest.raises(BridgeError) as caught:
        quota_resets.consume(store, account, "same-key")
    assert caught.value.code == "identity_changed"
    assert not any(method.endswith("/consume") for method, _ in state["calls"])


def test_pending_principal_change_keeps_unknown_outcome(setup):
    store, account, state = setup
    state["error"] = OSError("secret-provider-error")
    with pytest.raises(BridgeError):
        quota_resets.consume(store, account, "same-key")
    state["email"] = "someone-else@example.test"
    with pytest.raises(BridgeError) as caught:
        quota_resets.consume(Store(store.root), account, "same-key")
    assert caught.value.code == "unknown_outcome"
    assert caught.value.details["reason"] == "identity_changed"
    assert len([method for method, _ in state["calls"] if method.endswith("/consume")]) == 1


def test_unrecognized_provider_outcome_stays_pending(setup):
    store, account, state = setup
    state["outcome"] = "surprising-new-value"
    with pytest.raises(BridgeError) as caught:
        quota_resets.consume(store, account, "same-key")
    assert caught.value.code == "unknown_outcome"
    with store.connect() as db:
        row = db.execute("SELECT state,receipt FROM quota_reset_requests").fetchone()
    assert row["state"] == "submitted" and row["receipt"] is None


def test_concurrent_calls_do_not_submit_two_redemptions(setup):
    store, account, state = setup
    state.update(enter=threading.Event(), release=threading.Event())
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(quota_resets.consume(store, account, "same-key")))
    worker.start()
    assert state["enter"].wait(5)
    try:
        with pytest.raises(BridgeError) as caught:
            quota_resets.consume(Store(store.root), account, "same-key")
        assert caught.value.code == "account_busy"
    finally:
        state["release"].set()
        worker.join(timeout=5)
    assert not worker.is_alive() and outcomes[0]["outcome"] == "reset"
    assert len([method for method, _ in state["calls"] if method.endswith("/consume")]) == 1


@pytest.mark.parametrize("request_key,credit_id", [("", None), ("a b", None), ("x" * 201, None),
                                                   (True, None), ("good", "bad\ncredit")])
def test_invalid_inputs_never_launch_provider(setup, request_key, credit_id):
    store, account, state = setup
    with pytest.raises(BridgeError) as caught:
        quota_resets.consume(store, account, request_key, credit_id)
    assert caught.value.code == "invalid_input" and not state["calls"]


def test_other_engines_are_explicitly_unsupported(setup):
    store, account, state = setup
    with pytest.raises(BridgeError) as caught:
        quota_resets.consume(store, replace(account, engine="claude"), "key")
    assert caught.value.code == "unsupported_operation" and not state["calls"]


@pytest.mark.parametrize('uncertain', [False, True])
def test_submitting_reset_invalidates_previously_fresh_account_usage(setup, uncertain):
    store, account, state = setup
    observed = time.time()
    store.usage_observation(account.id, 'codex_app_server', 'account', {
        'supported': True, 'quota': {'rateLimits': {}, 'rateLimitResetCredits': {'availableCount': 1}}},
        observed_at=observed)
    bridge = Bridge(store.root)
    assert bridge.account_usage(account.id)['stale'] is False
    if uncertain:
        state['error'] = BridgeError('provider_timeout', 'The fixture lost its response.')
        with pytest.raises(BridgeError):
            quota_resets.consume(store, account, 'reset-with-cached-usage')
    else:
        quota_resets.consume(store, account, 'reset-with-cached-usage')
    usage = bridge.account_usage(account.id)
    assert usage['stale'] is True
    assert usage['reset_credits']['available_count'] == 1  # Historical observation, never rewritten.
    assert store.latest_usage_observation(account.id)['observed_at'] == observed


def test_pre_submission_rejection_does_not_block_a_new_request_after_login(setup):
    store, account, state = setup
    state['email'] = 'other@example.test'
    with pytest.raises(BridgeError) as caught:
        quota_resets.consume(store, account, 'before-login')
    assert caught.value.code == 'identity_changed' and caught.value.outcome == 'not_started'
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM quota_reset_requests').fetchone()[0] == 0
    state['email'] = account.email
    assert quota_resets.consume(store, account, 'after-login')['outcome'] == 'reset'
    assert len([method for method, _ in state['calls'] if method.endswith('/consume')]) == 1


def test_prepared_crash_can_rebind_but_submitted_request_cannot(setup):
    store, account, _ = setup
    row, _ = quota_resets._prepare(store, account, 'crashed-before-submit', None)
    changed = replace(account, home=account.home + '-reauthenticated')
    store.replace_account_home(changed)
    retried, replayed = quota_resets._prepare(store, changed, 'crashed-before-submit', None)
    assert replayed and retried['state'] == 'prepared'
    assert retried['binding'] != row['binding'] and retried['identity'] is None
    with store.connect() as db:
        db.execute("UPDATE quota_reset_requests SET state='submitted' WHERE request_key='crashed-before-submit'")
    store.replace_account_home(account)
    with pytest.raises(BridgeError) as caught:
        quota_resets._prepare(store, account, 'crashed-before-submit', None)
    assert caught.value.code == 'account_changed' and caught.value.outcome == 'unknown'
