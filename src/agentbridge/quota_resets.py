"""Explicit Codex reset redemption with a durable, replayable provider request.

Reading usage never calls this service. An uncertain redemption keeps its
original key and blocks a second logical reset until explicitly reconciled.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import time

from .account_probe import CodexAppServerProbe
from .errors import BridgeError
from .models import identifier
from .store import dumps


OUTCOMES = {"reset": "reset", "alreadyRedeemed": "already_redeemed",
            "nothingToReset": "nothing_to_reset", "noCredit": "no_credit"}


def _opaque(value, maximum, name):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise BridgeError("invalid_input", f"{name} must be bounded text without whitespace or control characters.")


@contextmanager
def _exclusive(store, account_id):
    path = store.root / (".quota-reset-" + identifier(account_id) + ".lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("account_busy", "A reset request for this account is already running.") from None
        yield
    finally:
        os.close(descriptor)


def _prepare(store, account, request_key, credit_id):
    fingerprint = hashlib.sha256(dumps(account.to_dict()).encode()).hexdigest()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS quota_reset_requests(
            request_key TEXT PRIMARY KEY, account_id TEXT NOT NULL, credit_id TEXT,
            binding TEXT NOT NULL, state TEXT NOT NULL, identity TEXT,
            receipt TEXT, created REAL NOT NULL, updated REAL NOT NULL)""")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS pending_quota_reset
            ON quota_reset_requests(account_id) WHERE state != 'completed'""")
        current = db.execute("SELECT config FROM accounts WHERE id=?", (account.id,)).fetchone()
        if current is None or json.loads(current["config"]) != json.loads(dumps(account.to_dict())):
            raise BridgeError("account_changed", "The selected account binding changed.")
        row = db.execute("SELECT * FROM quota_reset_requests WHERE request_key=?", (request_key,)).fetchone()
        if row:
            if row["account_id"] != account.id or row["credit_id"] != credit_id:
                raise BridgeError("idempotency_conflict", "This reset request key already has a different input.")
            if row["state"] == "submitted" and row["binding"] != fingerprint:
                raise BridgeError("account_changed", "The pending reset belongs to a different account binding.",
                                  outcome="unknown", details={"request_key": request_key})
            if row["state"] == "prepared" and row["binding"] != fingerprint:
                # A crash before submission cannot have consumed a credit.
                # Permit a newly authenticated binding, then verify its native
                # identity again before durably marking the request submitted.
                db.execute("UPDATE quota_reset_requests SET binding=?,identity=NULL,updated=? WHERE request_key=?",
                           (fingerprint, time.time(), request_key))
                row = db.execute("SELECT * FROM quota_reset_requests WHERE request_key=?", (request_key,)).fetchone()
            return dict(row), True
        pending = db.execute("SELECT request_key FROM quota_reset_requests WHERE account_id=? AND state != 'completed'",
                             (account.id,)).fetchone()
        if pending:
            raise BridgeError("reset_pending", "Reconcile the existing reset request before creating another.",
                              outcome="unknown", details={"request_key": pending["request_key"]})
        now = time.time()
        db.execute("""INSERT INTO quota_reset_requests(
            request_key,account_id,credit_id,binding,state,created,updated) VALUES (?,?,?,?,?,?,?)""",
                   (request_key, account.id, credit_id, fingerprint, "prepared", now, now))
        row = db.execute("SELECT * FROM quota_reset_requests WHERE request_key=?", (request_key,)).fetchone()
    return dict(row), False


def _principal(account, result):
    native = result.get("account")
    if not isinstance(native, dict) or native.get("type") != "chatgpt":
        raise BridgeError("authentication_required", "Reset redemption requires a signed-in ChatGPT account.")
    email = native.get("email")
    if (not isinstance(email, str) or not 1 <= len(email) <= 320
            or any(ord(char) < 32 for char in email)):
        raise BridgeError("identity_missing", "The provider did not identify the account for reset redemption.")
    email = email.strip().casefold()
    if not email or any(char.isspace() or ord(char) == 127 for char in email):
        raise BridgeError("identity_missing", "The provider did not identify the account for reset redemption.")
    if account.email and email != account.email.strip().casefold():
        raise BridgeError("identity_changed", "The provider account differs from the selected account.")
    value = {"email": email}
    account_id = native.get("accountId", native.get("account_id"))
    if isinstance(account_id, str) and 0 < len(account_id) <= 512:
        value["account_id"] = account_id
    return value


def _unknown(request_key, reason):
    return BridgeError("unknown_outcome", "The reset outcome is unknown. Reconcile using the same request key.",
                       phase="execution", outcome="unknown", retryable=False,
                       details={"request_key": request_key, "reason": reason})


def _discard_prepared(store, request_key):
    with store.connect() as db:
        db.execute("DELETE FROM quota_reset_requests WHERE request_key=? AND state='prepared'", (request_key,))


def consume(store, account, idempotency_key, credit_id=None):
    """Redeem only an explicit request, or replay its saved result after restart.

Only allowlisted outcomes enter the receipt. No provider bodies, credential
values, native paths or process output are returned or persisted here.
"""
    if account.engine != "codex":
        raise BridgeError("unsupported_operation", "Reset redemption is available only for Codex.")
    _opaque(idempotency_key, 200, "idempotency_key")
    if credit_id is not None:
        _opaque(credit_id, 512, "credit_id")
    with _exclusive(store, account.id):
        row, replayed = _prepare(store, account, idempotency_key, credit_id)
        if row["state"] == "completed":
            return {**json.loads(row["receipt"]), "replayed": True}
        probe = None
        uncertain = row["state"] == "submitted"
        try:
            from .provider_contracts import ContractRegistry
            ContractRegistry(store).check(account, enforce=True)
            probe = CodexAppServerProbe(account)
            probe._rpc("initialize", {"clientInfo": {"name": "agentbridge", "title": "AgentBridge", "version": "0.1"},
                                      "capabilities": {"experimentalApi": True}})
            probe._notify("initialized", {})
            principal = _principal(account, probe._rpc("account/read", {"refreshToken": False}))
            if row["identity"] and json.loads(row["identity"]) != principal:
                raise BridgeError("identity_changed", "The reset attempt belongs to a different provider account.")
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT config FROM accounts WHERE id=?", (account.id,)).fetchone()
                if current is None or json.loads(current["config"]) != json.loads(dumps(account.to_dict())):
                    raise BridgeError("account_changed", "The selected account binding changed before reset submission.")
                db.execute("UPDATE quota_reset_requests SET identity=?,state='submitted',updated=? WHERE request_key=?",
                           (dumps(principal), time.time(), idempotency_key))
                # Even a lost response can mean a credit was consumed. The
                # previous quota must stop looking current before submission.
                db.execute("UPDATE usage_observations SET stale=1 WHERE account_id=? AND scope='account'",
                           (account.id,))
            uncertain = True
            params = {"idempotencyKey": idempotency_key}
            if credit_id is not None:
                params["creditId"] = credit_id
            result = probe._rpc("account/rateLimitResetCredit/consume", params)
            outcome = OUTCOMES.get(result.get("outcome"))
            if outcome is None:
                raise BridgeError("provider_protocol_error", "The provider returned an invalid reset result.")
            now = time.time()
            receipt = {"account_id": account.id, "request_key": idempotency_key, "credit_id": credit_id,
                       "state": "completed", "outcome": outcome,
                       "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")}
            with store.connect() as db:
                db.execute("UPDATE quota_reset_requests SET state='completed',receipt=?,updated=? WHERE request_key=?",
                           (dumps(receipt), now, idempotency_key))
            return {**receipt, "replayed": replayed}
        except BridgeError as error:
            if uncertain:
                raise _unknown(idempotency_key, error.code) from None
            _discard_prepared(store, idempotency_key)
            raise
        except Exception:
            if uncertain:
                raise _unknown(idempotency_key, "provider_failed") from None
            _discard_prepared(store, idempotency_key)
            raise BridgeError("provider_failed", "The provider reset request could not be prepared.") from None
        finally:
            if probe is not None:
                probe.close()
