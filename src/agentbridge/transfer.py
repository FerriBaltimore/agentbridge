"""Conversation transfer policy kept outside the core bridge facade."""
import time
from pathlib import Path
from uuid import uuid4

from .continuity import all_events, unresolved
from .errors import BridgeError, BusyError, UnsupportedError
from .native import copy_session
from .store import dumps


class TransferMixin:
    def transfer(self, session_id, account_id, *, model=None, mode='auto', budget_bytes=128000,
                 target_workspace_path=None, validate_only=False, idempotency_key=None):
        """Create a branch without repeating it after a lost response."""
        if mode not in ('auto', 'native', 'portable'):
            raise BridgeError('invalid_mode', 'Choose auto, native or portable.')
        source = self.get_session(session_id)
        old = self.account(source['account_id'])
        target = self.resolve_account(account_id)
        target_workspace = Path(target_workspace_path).expanduser().resolve() if target_workspace_path else Path(source['cwd'])
        if not target_workspace.is_dir():
            raise BridgeError('invalid_workspace', 'Workspace must be an existing directory.')
        if target.id == old.id:
            raise BridgeError('same_account', 'Continue the existing session on the same account.')
        if validate_only:
            same_engine = old.engine == target.engine
            supported = mode != 'native' or (same_engine and old.engine in {'codex', 'claude'}
                                            and bool(source['native_id']) and bool(old.home) and bool(target.home))
            return {'supported': supported, 'mode': mode, 'same_engine': same_engine,
                    'reason': None if supported else 'native_continuation_unavailable'}
        target_model = model if model is not None else source['model'] if old.engine == target.engine else None
        if target.engine == 'cursor' and not target_model:
            raise BridgeError('model_required', 'Specify the destination Cursor model.')
        request_payload = dumps({'source': session_id, 'target': target.id, 'model': target_model,
                                 'mode': mode, 'workspace': str(target_workspace), 'budget': budget_bytes})
        replayed_id = None
        native_id = fallback = bundle = None
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if idempotency_key:
                previous = db.execute('SELECT session_id,payload FROM instance_requests WHERE request_key=?',
                                      (idempotency_key,)).fetchone()
                if previous:
                    if previous['payload'] != request_payload:
                        raise BridgeError('idempotency_conflict', 'Request key already belongs to different input.')
                    replayed_id = previous['session_id']
            if replayed_id is None:
                active = db.execute("SELECT 1 FROM runs WHERE account_id IN (?,?) AND state IN ('starting','running','stopping')",
                                    (old.id, target.id)).fetchone()
                if active:
                    raise BusyError()
                if mode != 'portable' and old.engine == target.engine and source['native_id'] and old.home and target.home:
                    try:
                        native_id = copy_session(old.engine, source['native_id'], old.home, target.home,
                                                 list(target.command or (target.engine,)))
                    except (BridgeError, OSError) as error:
                        if mode == 'native':
                            raise
                        fallback = getattr(error, 'code', 'native_unavailable')
                elif mode == 'native':
                    raise UnsupportedError('Native transfer requires the same supported engine and an observed native session.')
                bundle = None if native_id else self.export_context(session_id, budget_bytes=budget_bytes)
                unknown = unresolved(list(all_events(self.store, session_id)))
                context = bundle.text if bundle else ('Verify these unknown historical outcomes before repeating actions: '
                                                       + dumps(unknown) if unknown else None)
                replayed_id = uuid4().hex
                db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',
                           (replayed_id, target.id, str(target_workspace), target_model, native_id,
                            session_id, context, time.time()))
                db.execute('INSERT INTO instance_metadata(session_id,state,version,updated) VALUES (?,?,?,?)',
                           (replayed_id, 'active', 1, time.time()))
                if idempotency_key:
                    db.execute('INSERT INTO instance_requests(request_key,session_id,payload,created) VALUES (?,?,?,?)',
                               (idempotency_key, replayed_id, request_payload, time.time()))
        result = {**self._public_instance(self.get_session(replayed_id)),
                  'transfer_mode': 'native' if self.get_session(replayed_id).get('native_id') else 'portable',
                  'fallback_reason': fallback, 'context_omissions': len(bundle.omitted) if bundle else 0,
                  'replayed': native_id is None and fallback is None and bundle is None}
        return result
