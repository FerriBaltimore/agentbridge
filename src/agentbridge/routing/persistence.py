"""Transactional routing state, admission, and terminal reconciliation."""

from dataclasses import asdict
import json
import math
import sqlite3
import time

from ..errors import BridgeError, BusyError
from ..models import Account, RunOptions, TERMINAL, identifier, model_id
from .binding import has_bound_proxy_login
from .selector import RouteDecision
from .service import OBSERVATION_TTL, RoutingService


MAX_ROUTE_CONTEXT_BYTES = 128_000


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _route_evidence(decision, account_id, model):
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
    if state not in {"known", "unknown"} or reason not in {"least_used", "quota_unknown"}:
        raise BridgeError("invalid_request", "The route decision has an unsupported policy result.")
    if (state == "known" and reason != "least_used") or (state == "unknown" and reason != "quota_unknown"):
        raise BridgeError("invalid_request", "The route decision's reason and quota state disagree.")
    if ((state == "known" and (isinstance(used, bool) or not isinstance(used, (int, float))
                                or not math.isfinite(used) or not 0 <= used < 100))
            or (state == "unknown" and used is not None)):
        raise BridgeError("invalid_request", "The route quota observation is inconsistent.")
    return {"account_id": account_id, "model": model, "quota_state": state,
            "used_percent": used, "reason": reason}


def _route_context(context, omissions, *, required):
    if type(omissions) is not int or omissions < 0:
        raise BridgeError("invalid_request", "route_omissions must be a nonnegative count.")
    if required:
        if not isinstance(context, str) or not context:
            raise BridgeError("context_required", "This turn requires bounded portable context.")
        if len(context.encode("utf-8")) > MAX_ROUTE_CONTEXT_BYTES:
            raise BridgeError("context_over_budget", "Portable context exceeds the route budget.")
    elif context is not None or omissions:
        raise BridgeError("invalid_request", "Portable context is not required for this turn.")


def _reject_deleted_instance(db, session_id):
    if db.execute('SELECT 1 FROM deleted_instances WHERE session_id=?',
                  (session_id,)).fetchone():
        raise BridgeError('instance_deleted', 'A deleted instance cannot be recreated.')


def _session_payload(account_id, cwd, model, native_id, parent_id, context,
                     routing_mode, routing_provider=None, evaluation=False,
                     excluded_account_refs=()):
    payload = {"cwd": cwd, "model": model, "native_id": native_id,
               "parent_id": parent_id, "context": context}
    if routing_mode == "automatic":
        # Account selection can change before a lost-response retry arrives.
        payload["routing_mode"] = routing_mode
        if routing_provider is not None:
            payload["provider"] = routing_provider
        if excluded_account_refs:
            payload["excluded_account_refs"] = list(excluded_account_refs)
    else:
        # Keep the v3 pinned payload byte-for-byte stable across migration.
        payload = {"account_id": account_id, **payload}
    if evaluation:
        payload['evaluation'] = True
    return _dumps(payload)


def _verified_proxy_config(db, account_id, model, *, provider=None):
    if db.execute('SELECT 1 FROM paused_accounts WHERE account_id=?',
                  (account_id,)).fetchone():
        raise BridgeError('account_paused', 'The selected proxy account is paused for new work.')
    if db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',
                  (account_id,)).fetchone():
        raise BridgeError('account_retired', 'The selected proxy account was retired.')
    row = db.execute("SELECT config FROM accounts WHERE id=?", (account_id,)).fetchone()
    if row is None:
        raise BridgeError("account_not_found", "The selected account does not exist.")
    config = json.loads(row["config"])
    if provider is not None and config.get("provider") != provider:
        raise BridgeError("provider_unavailable", "The selected account belongs to another provider.")
    if config.get("engine") != "codex" or not config.get("proxy_base_url"):
        raise BridgeError("invalid_proxy_account", "Execution requires a Codex account with a local proxy.")
    if not has_bound_proxy_login(db, config):
        raise BridgeError("proxy_binding_unverified", "This account has no completed GrantBridge proxy login.")
    if model is None:
        raise BridgeError("model_required", "Choose a model for proxy routing.")
    model_id(model)
    if model not in config.get("supported_models", []):
        raise BridgeError("model_unavailable", "The proxy account does not declare this model.")
    if not config.get("management_key_env"):
        raise BridgeError("proxy_binding_unverified", "The proxy account has no management key reference.")
    binding = db.execute("SELECT 1 FROM proxy_bindings WHERE account_id=?", (account_id,)).fetchone()
    observed = db.execute("SELECT observed_at,source,data FROM account_observations "
                          "WHERE account_id=? ORDER BY observed_at DESC,id DESC LIMIT 1",
                          (account_id,)).fetchone()
    if binding is None or observed is None or observed["source"] != "cliproxy_management":
        raise BridgeError("proxy_binding_unverified", "The proxy account binding is unverified.")
    age = time.time() - observed["observed_at"]
    if not 0 <= age < OBSERVATION_TTL:
        raise BridgeError("proxy_binding_unverified", "The proxy account binding is stale.")
    saved = {"data": json.loads(observed["data"])}
    if not RoutingService._verified(Account(**config), saved):
        raise BridgeError("proxy_binding_unverified", "The proxy account binding is unverified.")
    if model not in {item.get("id") for item in saved["data"]["models"]
                     if isinstance(item, dict)}:
        raise BridgeError("model_unavailable", "The proxy does not currently expose this model.")


class RoutingStoreMixin:
    def add_session(self, id, account_id, cwd, model, native_id=None, parent_id=None,
                    context=None, request_key=None, routing_mode="pinned",
                    routing_provider=None, evaluation=False, excluded_account_refs=()):
        if type(evaluation) is not bool:
            raise BridgeError('invalid_request', 'evaluation must be a boolean.')
        if evaluation and (native_id is not None or parent_id is not None or context is not None):
            raise BridgeError('invalid_request', 'Evaluations require a fresh instance.')
        if routing_mode not in {"pinned", "automatic"}:
            raise BridgeError("invalid_mode", "Choose pinned or automatic routing.")
        if routing_mode == "automatic":
            if native_id is not None or context is not None or model is None:
                raise BridgeError("invalid_request", "Automatic sessions require a model and no initial native context.")
            model_id(model)
            if routing_provider is not None:
                identifier(routing_provider)
        elif routing_provider is not None:
            raise BridgeError("invalid_request", "Provider routing requires an automatic instance.")
        encoded = _session_payload(account_id, cwd, model, native_id, parent_id,
                                   context, routing_mode, routing_provider, evaluation,
                                   excluded_account_refs)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key:
                old = db.execute("SELECT session_id,payload FROM instance_requests WHERE request_key=?",
                                 (request_key,)).fetchone()
                if old:
                    _reject_deleted_instance(db, old['session_id'])
                    discarded = db.execute("SELECT 1 FROM evaluation_instances WHERE session_id=? "
                        "AND status IN ('discarding','discarded')", (old['session_id'],)).fetchone()
                    if discarded:
                        raise BridgeError('evaluation_discarded',
                                          'A discarded evaluation cannot be recreated.')
                    if old["payload"] != encoded:
                        raise BridgeError("idempotency_conflict", "Request key already belongs to different input.")
                    return old["session_id"], False
            _reject_deleted_instance(db, id)
            _verified_proxy_config(db, account_id, model, provider=routing_provider)
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                       (id, account_id, cwd, model, native_id, parent_id, context, time.time()))
            db.execute("INSERT INTO instance_metadata(session_id,state,version,updated) VALUES (?,?,?,?)",
                       (id, "active", 1, time.time()))
            db.execute("INSERT INTO session_routing(session_id,mode,last_completed_account_id,last_native_id,provider) "
                       "VALUES (?,?,?,?,?)", (id, routing_mode,
                       account_id if routing_mode == "pinned" else None,
                       native_id if routing_mode == "pinned" else None, routing_provider))
            if evaluation:
                db.execute('INSERT INTO evaluation_instances(session_id,account_id,status,created) '
                           'VALUES (?,?,?,?)', (id, account_id, 'active', time.time()))
            if request_key:
                db.execute("INSERT INTO instance_requests(request_key,session_id,payload,created) VALUES (?,?,?,?)",
                           (request_key, id, encoded, time.time()))
        return id, True

    def replay_auto_session(self, request_key, cwd, model, *, provider=None, evaluation=False,
                            excluded_account_refs=()):
        """Resolve a lost automatic-create response before routing side effects."""
        if not request_key:
            return None
        expected = _session_payload(None, cwd, model, None, None, None, "automatic", provider,
                                    evaluation, excluded_account_refs)
        with self.connect() as db:
            row = db.execute("SELECT session_id,payload FROM instance_requests WHERE request_key=?",
                             (request_key,)).fetchone()
            if row:
                _reject_deleted_instance(db, row['session_id'])
            discarded = (db.execute("SELECT 1 FROM evaluation_instances WHERE session_id=? "
                "AND status IN ('discarding','discarded')", (row['session_id'],)).fetchone()
                if row else None)
        if row is None:
            return None
        if discarded:
            raise BridgeError('evaluation_discarded', 'A discarded evaluation cannot be recreated.')
        if row["payload"] != expected:
            raise BridgeError("idempotency_conflict", "Request key already belongs to different input.")
        return row["session_id"]

    def replay_pinned_session(self, request_key, account_id, cwd, model, *, evaluation=False):
        """Return an admitted pinned session without rechecking a possibly offline proxy."""
        if not request_key:
            return None
        expected = _session_payload(account_id, cwd, model, None, None, None, "pinned",
                                    evaluation=evaluation)
        with self.connect() as db:
            row = db.execute("SELECT session_id,payload FROM instance_requests WHERE request_key=?",
                             (request_key,)).fetchone()
            if row:
                _reject_deleted_instance(db, row['session_id'])
            discarded = (db.execute("SELECT 1 FROM evaluation_instances WHERE session_id=? "
                "AND status IN ('discarding','discarded')", (row['session_id'],)).fetchone()
                if row else None)
        if row is None:
            return None
        if discarded:
            raise BridgeError('evaluation_discarded', 'A discarded evaluation cannot be recreated.')
        if row["payload"] != expected:
            raise BridgeError("idempotency_conflict", "Request key already belongs to different input.")
        return row["session_id"]

    def routing(self, session_id):
        with self.connect() as db:
            row = db.execute("SELECT mode,last_completed_account_id,last_native_id,provider "
                             "FROM session_routing WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise BridgeError("instance_not_found", "Instance does not exist.")
        return dict(row)

    def route_load(self, account_ids):
        ids = tuple(account_ids)
        if len(ids) > 1000 or len(set(ids)) != len(ids):
            raise BridgeError("invalid_request", "Route account IDs must be unique and bounded.")
        for account_id in ids:
            identifier(account_id)
        result = {account_id: {"in_flight": 0, "assigned_turns": 0} for account_id in ids}
        if not ids:
            return result
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as db:
            rows = db.execute(f"""SELECT account_id, COUNT(*) AS assigned_turns,
                    SUM(CASE WHEN state IN ('starting','running','stopping') THEN 1 ELSE 0 END) AS in_flight
                    FROM runs WHERE account_id IN ({placeholders}) GROUP BY account_id""", ids).fetchall()
        for row in rows:
            result[row["account_id"]] = {"in_flight": row["in_flight"],
                                         "assigned_turns": row["assigned_turns"]}
        return result

    def last_route_event_seq(self, session_id):
        with self.connect() as db:
            row = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE session_id=?",
                             (session_id,)).fetchone()
        return row[0]

    def admit(self, id, session_id, prompt, options, key, message_id=None, *,
              account_id=None, route_decision=None, route_context=None, route_omissions=0,
              route_event_seq=None, excluded_account_refs=()):
        message_id = message_id or id
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                old = db.execute("SELECT * FROM runs WHERE request_key=?", (key,)).fetchone()
                if old:
                    old_options = _dumps(asdict(RunOptions(**json.loads(old["options"]))))
                    if (old["session_id"], old["prompt"], old_options) != (
                            session_id, prompt, _dumps(asdict(options))):
                        raise BridgeError("idempotency_conflict", "Request key already belongs to different input.")
                    marker = db.execute('SELECT refs FROM run_route_exclusions WHERE run_id=?',
                                        (old['id'],)).fetchone()
                    if (marker['refs'] if marker else '[]') != _dumps(list(excluded_account_refs)):
                        raise BridgeError('idempotency_conflict',
                                          'Request key already belongs to different input.')
                    return old["id"], False
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session:
                raise BridgeError("not_found", "Session does not exist.")
            metadata = db.execute("SELECT state FROM instance_metadata WHERE session_id=?",
                                  (session_id,)).fetchone()
            if metadata and metadata["state"] == "archived":
                raise BridgeError("instance_archived", "Archived instances cannot accept new messages.")
            evaluation = db.execute('SELECT status FROM evaluation_instances WHERE session_id=?',
                                    (session_id,)).fetchone()
            if evaluation is not None:
                if evaluation['status'] != 'active' or db.execute(
                        'SELECT 1 FROM runs WHERE session_id=?', (session_id,)).fetchone():
                    raise BridgeError('evaluation_consumed',
                                      'An evaluation instance accepts only one turn.')
            routing = db.execute("SELECT * FROM session_routing WHERE session_id=?", (session_id,)).fetchone()
            if routing is None:
                raise BridgeError("schema_version", "Session routing metadata is missing.")
            selected = session["account_id"]
            route_event = None
            if routing["mode"] == "automatic":
                if account_id is None:
                    raise BridgeError("invalid_request", "Automatic routing requires an account selection.")
                identifier(account_id)
                model = options.model or session["model"]
                _verified_proxy_config(db, account_id, model, provider=routing["provider"])
                route_event = _route_evidence(route_decision, account_id, model)
                prior = db.execute("SELECT account_id,state FROM runs WHERE session_id=? "
                                   "ORDER BY rowid DESC LIMIT 1", (session_id,)).fetchone()
                stable = routing["last_completed_account_id"]
                previous = prior["account_id"] if prior is not None else None
                switched = ((previous is not None and previous != account_id)
                            or (stable is not None and stable != account_id))
                needs_context = (prior is not None and
                                 (switched or routing["last_native_id"] is None
                                  or prior["state"] != "completed"))
                _route_context(route_context, route_omissions, required=needs_context)
                if needs_context:
                    if type(route_event_seq) is not int or route_event_seq < 0:
                        raise BridgeError("invalid_request", "Portable context requires an event snapshot.")
                    current_seq = db.execute(
                        "SELECT COALESCE(MAX(seq),0) FROM events WHERE session_id=?",
                        (session_id,)).fetchone()[0]
                    if current_seq != route_event_seq:
                        raise BridgeError("context_stale", "Conversation evidence changed before admission.",
                                          retryable=True)
                elif route_event_seq is not None:
                    raise BridgeError("invalid_request", "An event snapshot is not needed for this turn.")
                route_event.update({"previous_account_id": previous,
                                    "last_completed_account_id": stable,
                                    "account_changed": switched,
                                    "portable_context_used": needs_context,
                                    "context_omitted_count": route_omissions,
                                    "context_bytes": len(route_context.encode("utf-8")) if needs_context else 0})
                selected = account_id
            elif any(value is not None for value in (account_id, route_decision, route_context,
                                                     route_event_seq)) or route_omissions:
                raise BridgeError("invalid_request", "Pinned sessions do not accept automatic routing fields.")
            else:
                _verified_proxy_config(db, selected, options.model or session["model"])
            now = time.time()
            try:
                db.execute("""INSERT INTO runs
                    (id,message_id,session_id,account_id,state,prompt,options,request_key,created,updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                           (id, message_id, session_id, selected, "starting", prompt,
                            _dumps(asdict(options)), key, now, now))
            except sqlite3.IntegrityError as error:
                raise BusyError() from error
            if excluded_account_refs:
                db.execute('INSERT INTO run_route_exclusions(run_id,refs) VALUES (?,?)',
                           (id, _dumps(list(excluded_account_refs))))
            if route_event is not None:
                if route_event["portable_context_used"]:
                    db.execute("UPDATE sessions SET account_id=?,native_id=NULL,context=? WHERE id=?",
                               (selected, route_context, session_id))
                else:
                    db.execute("UPDATE sessions SET account_id=? WHERE id=?", (selected, session_id))
                self._event(db, id, session_id, "route_selected", route_event)
            from ..attachments import descriptors
            self._event(db, id, session_id, "user",
                        {"text": prompt, "attachments": descriptors(options.attachments),
                         "attachment_content_omitted": bool(options.attachments)})
        return id, True

    def finish(self, id, state, code=None, exit_code=None):
        if state not in TERMINAL:
            raise ValueError(state)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (id,)).fetchone()
            if row is None:
                raise BridgeError("not_found", "Run does not exist.")
            if row["state"] in TERMINAL:
                return
            if row["stop_requested"] and state != "interrupted":
                state, code = "cancelled", "user_stop"
            from ..turn_outcome import completion
            self._event(db, id, row["session_id"], "run_finished",
                        completion(db, id, state, code, exit_code))
            db.execute("UPDATE runs SET state=?,error=?,exit_code=?,updated=? WHERE id=?",
                       (state, code, exit_code, time.time(), id))
            routing = db.execute("SELECT * FROM session_routing WHERE session_id=?",
                                 (row["session_id"],)).fetchone()
            if state == "completed":
                current = db.execute("SELECT native_id FROM sessions WHERE id=?",
                                     (row["session_id"],)).fetchone()
                db.execute("UPDATE session_routing SET last_completed_account_id=?,last_native_id=? "
                           "WHERE session_id=?", (row["account_id"], current["native_id"], row["session_id"]))
            elif routing is not None and routing["mode"] == "automatic":
                previous = routing["last_completed_account_id"]
                if previous is None:
                    db.execute("UPDATE sessions SET native_id=NULL WHERE id=?", (row["session_id"],))
                else:
                    db.execute("UPDATE sessions SET account_id=?,native_id=? WHERE id=?",
                               (previous, routing["last_native_id"], row["session_id"]))
