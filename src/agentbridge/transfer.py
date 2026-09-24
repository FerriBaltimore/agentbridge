"""Conversation transfer policy kept outside the core bridge facade."""
import time
from uuid import uuid4

from .errors import BridgeError, BusyError, UnsupportedError
from .store import dumps
from .workspace_policy import validate_workspace


class TransferMixin:
    def transfer(self, session_id, account_id, *, model=None, mode='auto', budget_bytes=128000,
                 target_workspace_path=None, validate_only=False, idempotency_key=None):
        """Create a branch without repeating it after a lost response."""
        if mode not in ('auto', 'native', 'portable'):
            raise BridgeError('invalid_mode', 'Choose auto, native or portable.')
        source = self.get_session(session_id)
        if source.get('evaluation'):
            raise BridgeError('evaluation_instance',
                              'Evaluation instances cannot be transferred.')
        old = self.account(source['account_id'])
        target = self.resolve_account(account_id)
        from .transports import require_proxy_account
        require_proxy_account(target)
        target_workspace = validate_workspace(
            target_workspace_path or source['cwd'], self.store.root)
        if target.id == old.id:
            raise BridgeError('same_account', 'Continue the existing session on the same account.')
        if mode == 'native':
            if validate_only:
                return {'supported': False, 'mode': 'portable', 'same_engine': True,
                        'reason': 'native_continuation_unavailable'}
            raise UnsupportedError('Cross-account continuation uses portable context.')
        target_model = model if model is not None else source['model'] if old.engine == target.engine else None
        from .routing.admission import verify_proxy_model
        if validate_only:
            try:
                verify_proxy_model(self.routes, target, target_model, refresh=True)
            except BridgeError as error:
                return {'supported': False, 'mode': 'portable', 'same_engine': True,
                        'reason': error.code}
            return {'supported': True, 'mode': 'portable', 'same_engine': True,
                    'reason': None}
        request_payload = dumps({'source': session_id, 'target': target.id, 'model': target_model,
                                 'mode': mode, 'workspace': str(target_workspace), 'budget': budget_bytes})
        if idempotency_key:
            with self.store.connect() as db:
                previous = db.execute('SELECT session_id,payload FROM instance_requests WHERE request_key=?',
                                      (idempotency_key,)).fetchone()
            if previous:
                if previous['payload'] != request_payload:
                    raise BridgeError('idempotency_conflict', 'Request key already belongs to different input.')
                return {**self._public_instance(self.get_session(previous['session_id'])),
                        'transfer_mode': 'portable', 'fallback_reason': None,
                        'context_omissions': 0, 'replayed': True}
        verify_proxy_model(self.routes, target, target_model, refresh=True)
        replayed_id = None
        bundle = None
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
                bundle = self.export_context(session_id, budget_bytes=budget_bytes)
                context = bundle.text
                replayed_id = uuid4().hex
                db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',
                           (replayed_id, target.id, str(target_workspace), target_model, None,
                            session_id, context, time.time()))
                db.execute('INSERT INTO instance_metadata(session_id,state,version,updated) VALUES (?,?,?,?)',
                           (replayed_id, 'active', 1, time.time()))
                db.execute('INSERT INTO session_routing(session_id,mode,last_completed_account_id,last_native_id) '
                           'VALUES (?,?,?,?)', (replayed_id, 'pinned', target.id, None))
                if idempotency_key:
                    db.execute('INSERT INTO instance_requests(request_key,session_id,payload,created) VALUES (?,?,?,?)',
                               (idempotency_key, replayed_id, request_payload, time.time()))
        result = {**self._public_instance(self.get_session(replayed_id)),
                  'transfer_mode': 'portable', 'fallback_reason': None,
                  'context_omissions': len(bundle.omitted) if bundle else 0,
                  'replayed': bundle is None}
        return result
