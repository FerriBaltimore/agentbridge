"""Public SDK. No server, event loop or Fullbrain installation required."""
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from .continuity import build, unresolved
from .discovery import DiscoveryMixin
from .event_contract import public_event
from .errors import BridgeError, BusyError, UnsupportedError
from .models import Account, RunOptions, TERMINAL, identifier, page_values
from .message_submission import MessageSubmissionMixin
from .transfer import TransferMixin
from .process import alive
from .store import Store
from . import usage
from .accounts import AccountService
from .authentication import AuthenticationService
from .run_state import Run
from .transcript import messages as transcript_messages
from .error_management import ErrorManagementMixin
from .provider_contracts import ContractRegistry
from .routing.service import RoutingService
from .proxy.managed import ManagedProxyClient
from .execution_context import verify
from .evaluation.service import EvaluationMixin


class Bridge(EvaluationMixin, MessageSubmissionMixin, DiscoveryMixin, TransferMixin, ErrorManagementMixin):
    def __init__(self, root='.agentbridge'):
        if os.name!='posix':raise UnsupportedError('Process supervision currently requires a POSIX host.')
        self.store=Store(root)
        self.account_service=AccountService(self.store)
        self.managed_proxy=ManagedProxyClient(self.store.root)
        self.routes=RoutingService(self.store, self.account_service, self.managed_proxy)
        self.authentication=AuthenticationService(self.store, self.account_service,
                                                  self.managed_proxy)
        self._children={}

    @property
    def root(self):return self.store.root

    def register(self, account: Account):
        raise BridgeError('authentication_required', 'Create accounts through the proxy login flow.')

    def accounts(self, **filters):
        if filters.get('engine') is not None:
            raise BridgeError('unsupported_parameter', 'Accounts are selected by provider and model.')
        return self.account_service.list(**filters)

    def account(self, id):
        return self.account_service.get(id)

    def resolve_account(self, reference):
        return self.account_service.resolve(reference)

    def account_delete(self, account_ref):
        account = self.account_service.resolve(account_ref, include_retired=True)
        result = self.store.retire_account(account.id)
        if self.managed_proxy.is_managed(account.to_dict(), account.id):
            try:
                self.managed_proxy.retire(account.id)
            except BridgeError:
                pass
        return result

    def account_status(self, account_id=None, *, account_ref=None, refresh=False):
        account = self.account(self._account_id(account_id, account_ref))
        if account.id in self.store.retired_account_ids():
            return self.account_service.status(account.id, refresh=False)
        if account.proxy_base_url:
            if refresh or self.managed_proxy.is_managed(account.to_dict(), account.id):
                self.routes.observation(account, refresh=refresh)
            return self.account_service.status(account.id, refresh=False)
        result = self.account_service.status(account.id, refresh=False)
        result['authentication'] = {**result['authentication'], 'status': 'legacy_read_only'}
        result['reason'] = 'legacy_read_only'
        return result

    def account_usage(self, account_id=None, *, account_ref=None, refresh=False):
        account = self.account(self._account_id(account_id, account_ref))
        if account.id in self.store.retired_account_ids():
            return {'account_id': account.id, 'scope': 'account', 'supported': False,
                    'stale': True, 'reason': 'account_removed'}
        if account.proxy_base_url:
            return self.routes.usage(account, refresh=refresh)
        return {'account_id': account.id, 'scope': 'account', 'supported': False,
                'stale': True, 'reason': 'legacy_read_only'}

    def usage(self, scope='account', *, account_ref=None, instance_id=None, turn_id=None,
              refresh=False, include_quota=False, since=None, until=None):
        if since or until:
            raise UnsupportedError('Usage time filters are not supported by this adapter.')
        if scope == 'account':
            account_id = self._account_id(None, account_ref)
            value = self.account_usage(account_id, refresh=refresh)
            # Account usage already includes quota. Never replace a live snapshot
            # with the older rollout-only compatibility surface.
            return {**value, 'scope': 'account'}
        if scope == 'turn':
            if not turn_id:
                raise BridgeError('turn_id_required', 'turn_id is required for turn usage.')
            return {'scope': 'turn', 'turn_id': turn_id, **self.run(turn_id).consumption}
        if scope == 'instance':
            if not instance_id:
                raise BridgeError('instance_id_required', 'instance_id is required for instance usage.')
            rows = self.store.session_runs(instance_id, limit=10001)
            partial = len(rows) > 10000
            rows = rows[:10000]
            observations = [self.run(row['id']).consumption for row in rows]
            return {'scope': 'instance', 'instance_id': instance_id,
                    'turns': len(rows), 'observations': observations,
                    'partial': partial, 'observation_limit': 10000,
                    'supported': any(item.get('supported') for item in observations)}
        raise BridgeError('invalid_scope', 'scope must be account, instance or turn.')

    def account_usage_history(self, account_id, *, limit=100, cursor=0, since=None,
                              until=None, granularity=None, refresh=False):
        limit, cursor = page_values(limit, cursor)
        if since or until or granularity:
            raise UnsupportedError('Historical usage filters are not supported by this adapter.')
        values = self.account_service.history(account_id, limit=limit + cursor)
        return values[cursor:cursor + limit]

    def account_quota_reset(self, account_ref, *, idempotency_key, credit_id=None):
        raise UnsupportedError('The local proxy does not expose Codex earned reset redemption.')

    def account_login(self, **options):return self.authentication.login(**options)
    def account_login_start(self, **options):return self.authentication.start(**options)
    def account_login_check(self, attempt_id=None, **options):return self.authentication.check(attempt_id or options.pop('attempt_id'), **options)
    def account_login_complete(self, attempt_id=None, **options):return self.authentication.complete(attempt_id or options.pop('attempt_id'), **options)
    def account_login_status(self, attempt_id=None, **options):return self.authentication.status(attempt_id or options.pop('attempt_id'), **options)
    def account_login_cancel(self, attempt_id=None, **options):return self.authentication.cancel(attempt_id or options.pop('attempt_id'), **options)
    def account_login_callback(self, attempt_id=None, **options):
        return self.authentication.callback(attempt_id or options.pop('attempt_id'), **options)

    def _account_id(self, account_id, account_ref):
        if account_id or account_ref:
            return self.account_service.resolve(account_ref or account_id,
                                                include_retired=True).id
        raise BridgeError('account_ref_required', 'account_ref is required.')

    def _public_instance(self, value):
        result = dict(value)
        result['instance_id'] = result['id']
        routing = self.store.routing(result['id'])
        result['routing_mode'] = routing['mode']
        result['routing_provider'] = routing.get('provider') if routing['mode'] == 'automatic' else None
        account = self.account(result['account_id'])
        result['account_ref'] = account.name or result['account_id']
        result['workspace_path'] = result['cwd']
        result['native_session_id'] = result.get('native_id')
        result['created_at'] = result['created']
        result['updated_at'] = result.get('updated', result['created'])
        return result

    def session(self, account_id, cwd, *, model=None, request_key=None, evaluation=False):
        account=self.account(account_id)
        cwd=str(Path(cwd).expanduser().resolve())
        if not Path(cwd).is_dir():raise BridgeError('invalid_workspace','Workspace must be an existing directory.')
        replayed = self.store.replay_pinned_session(request_key, account.id, cwd, model,
                                                    evaluation=evaluation)
        if replayed:
            return {**self.get_session(replayed), 'replayed': True}
        from .routing.admission import verify_proxy_model
        verify_proxy_model(self.routes, account, model, refresh=True)
        id=uuid4().hex
        session_id, created = self.store.add_session(id, account_id, cwd, model,
                                                     request_key=request_key,
                                                     evaluation=evaluation)
        result = self.get_session(session_id)
        result['replayed'] = not created
        return result

    def instance_create(self, *, engine=None, account_ref=None, provider=None,
                        workspace_path=None, model=None,
                        effort=None, context_window=None, permission_mode='dontAsk',
                        sandbox_mode='read-only', allowed_tools=(), metadata=None,
                        provider_options=None, continuity_mode=None, idempotency_key=None,
                        evaluation=False):
        if type(evaluation) is not bool:
            raise BridgeError('invalid_request', 'evaluation must be a boolean.')
        if provider_options or metadata or continuity_mode:
            raise UnsupportedError('Provider-specific options, metadata and continuity require an adapter contract.')
        if effort or context_window or permission_mode != 'dontAsk' or sandbox_mode != 'read-only' or allowed_tools:
            raise UnsupportedError('Instance defaults are configured per turn by this adapter.')
        if engine is not None:
            raise BridgeError('unsupported_parameter', 'Execution always uses the routed Codex engine.')
        if account_ref is not None and provider is not None:
            raise BridgeError('unsupported_parameter',
                              'provider filters automatic routing only.')
        if account_ref is None:
            from .routing.admission import create_automatic_instance
            return create_automatic_instance(self, workspace_path=workspace_path, model=model,
                                             idempotency_key=idempotency_key,
                                             provider=provider, evaluation=evaluation)
        account = self.resolve_account(account_ref)
        result = self.session(account.id, workspace_path or '.', model=model,
                              request_key=idempotency_key, evaluation=evaluation)
        return self._public_instance(result)

    def instance_get(self, instance_id, *, include_last_turn=False, include_usage=False):
        value = self._public_instance(self.get_session(instance_id))
        if include_last_turn:
            value['last_turn'] = self.store.last_session_run(instance_id)
        if include_usage:
            value['usage'] = {'turns': self.store.session_run_count(instance_id),
                              'observed': False, 'reason': 'run_usage_available_per_turn'}
        return value

    def instances(self, *, engine=None, account_ref=None, state=None, limit=100, cursor=0, include_last_turn=False):
        if engine is not None:
            raise BridgeError('unsupported_parameter', 'Instances are selected by model and account.')
        limit, cursor = page_values(limit, cursor)
        account_id = self.resolve_account(account_ref).id if account_ref else None
        rows = self.store.list('sessions')
        selected = [row for row in rows if (not account_id or row['account_id'] == account_id)
                    and (not state or row.get('state') == state)]
        selected = selected[cursor:cursor + limit]
        if include_last_turn:
            for row in selected:
                row['last_turn'] = self.store.last_session_run(row['id'])
        return [self._public_instance(row) for row in selected]

    def instance_update(self, instance_id, *, model=None, effort=None, context_window=None,
                        permission_mode=None, sandbox_mode=None, allowed_tools=None,
                        expected_version=None, metadata=None, provider_options=None, state=None):
        if any(value is not None for value in (effort, context_window, permission_mode,
                                                sandbox_mode, allowed_tools, metadata, provider_options)):
            raise UnsupportedError('Only model and state updates are supported by this adapter.')
        values = {}
        if model is not None:
            current = self.get_session(instance_id)
            routing = self.store.routing(instance_id)
            if routing['mode'] == 'automatic':
                declared = any(account.proxy_base_url and model in account.supported_models
                               and (routing.get('provider') is None
                                    or account.provider == routing['provider'])
                               for account in self.accounts())
            else:
                account = self.account(current['account_id'])
                declared = bool(account.proxy_base_url and model in account.supported_models)
            if not declared:
                raise BridgeError('model_unavailable', 'No account declares support for this model.')
            values['model'] = model
        if state is not None:
            values['state'] = state
        if not values:
            raise BridgeError('invalid_request', 'At least one instance field must change.')
        return self._public_instance(self.store.update_session(instance_id, expected_version=expected_version, **values))

    def instance_archive(self, instance_id, *, expected_version=None):
        return self._public_instance(self.store.update_session(instance_id, expected_version=expected_version, state='archived'))

    def messages(self, instance_id, *, after=None, before=None, role=None, limit=100, cursor=0):
        return transcript_messages(self, instance_id, after=after, before=before, role=role,
                                   limit=limit, cursor=cursor)

    def turn(self, turn_id, *, include_usage=False, include_error=False):
        run = self.run(turn_id)
        value = dict(run.snapshot)
        value['turn_id'] = value['id']
        value['instance_id'] = value['session_id']
        value['message_id'] = value.get('message_id', value['id'])
        value['created_at'] = value['created']
        value['updated_at'] = value['updated']
        value['account_ref'] = self.account(value['account_id']).name or value['account_id']
        from .turn_outcome import detail
        with self.store.connect() as db:
            issue = detail(db, turn_id, value['state'], value.get('error'))
        value['outcome'] = issue['outcome'] if issue else value['state']
        value['provider_compatibility'] = ContractRegistry(self.store).run(turn_id)
        if include_usage:
            value['usage'] = run.consumption
        if include_error and value.get('error'):
            value['error_detail'] = issue
        return value

    def turns(self, *, instance_id=None, state=None, limit=100, cursor=0):
        limit, cursor = page_values(limit, cursor)
        rows = self.runs()
        selected = [row for row in rows
                    if (not instance_id or row['session_id'] == instance_id)
                    and (not state or row['state'] == state)]
        return [dict(row, turn_id=row['id']) for row in selected[cursor:cursor + limit]]

    def turn_events(self, turn_id, *, after_seq=0, limit=1000, follow=False, timeout_ms=None):
        if timeout_ms is not None and (not isinstance(timeout_ms, (int, float))
                                       or isinstance(timeout_ms, bool) or timeout_ms < 0):
            raise BridgeError('invalid_timeout', 'timeout_ms must be nonnegative.')
        run = self.run(turn_id)
        engine = self.account(run.session['account_id']).engine
        return [public_event(event, engine) for event in run.events(
            after=after_seq, limit=limit, follow=follow,
            timeout=(timeout_ms / 1000) if timeout_ms is not None else None)]

    def instance_events(self, instance_id, *, after_seq=0, limit=1000, follow=False, timeout_ms=None):
        if follow:
            raise UnsupportedError('Conversation event follow is not supported; poll with after_seq.')
        if timeout_ms is not None and (not isinstance(timeout_ms, (int, float))
                                       or isinstance(timeout_ms, bool) or timeout_ms < 0):
            raise BridgeError('invalid_timeout', 'timeout_ms must be nonnegative.')
        session = self.get_session(instance_id)
        engine = self.account(session['account_id']).engine
        events = self.store.events(session_id=instance_id, after=after_seq, limit=limit)
        return [public_event(event, engine) for event in events]

    def turn_stop(self, turn_id, *, reason=None, grace_period_ms=None, wait=False):
        if grace_period_ms is not None and (not isinstance(grace_period_ms, (int, float))
                                            or isinstance(grace_period_ms, bool) or grace_period_ms < 0):
            raise BridgeError('invalid_timeout', 'grace_period_ms must be nonnegative.')
        if grace_period_ms is not None:
            raise UnsupportedError('Stop grace is configured by RunOptions.stop_grace before execution.')
        self.run(turn_id).stop(wait=wait, timeout=15)
        result = self.turn(turn_id)
        if reason:
            result['stop_reason'] = reason
        return result

    def turn_resume(self, turn_id, content=None, *, mode='native', timeout_ms=None, idempotency_key=None):
        if mode not in ('native', 'reconcile'):
            raise BridgeError('invalid_mode', 'Choose native or reconcile.')
        run = self.run(turn_id)
        if mode == 'reconcile' and run.status not in TERMINAL:
            raise BusyError()
        options = None
        if timeout_ms is not None:
            values = json.loads(run.snapshot['options'])
            values['timeout'] = timeout_ms / 1000
            options = RunOptions(**values)
        resumed = run.resume(prompt=content, options=options, request_key=idempotency_key)
        return {'turn_id': resumed.id, 'instance_id': resumed.snapshot['session_id'],
                'state': resumed.status, 'mode': mode}

    def permission_respond(self, turn_id, permission_id, decision, *, reason=None, expires_at=None):
        from .permissions import Permissions
        from .transports import duplex
        run = self.run(turn_id)
        if not duplex(self.account(run.snapshot['account_id']), RunOptions(**json.loads(run.snapshot['options']))):
            raise UnsupportedError('This turn has no interactive permission transport.')
        return Permissions(self.store).respond(turn_id, permission_id, decision,
                                               reason=reason, expires_at=expires_at)

    def get_session(self, id):return self.store.get('sessions',identifier(id))
    def sessions(self):return self.store.list('sessions')
    def runs(self):return self.store.list('runs')
    def run(self, id):
        self.store.get('runs',identifier(id))
        return Run(self,id)

    def submit(self, session_id, prompt, *, options=None, request_key=None, message_id=None, execution=None):
        options=options or RunOptions()
        if execution is not None or options.context_package_digest or options.mcp_binding_digest: verify(options, execution)
        if not isinstance(prompt,str) or not prompt.strip():raise BridgeError('empty_prompt','A nonempty prompt is required.')
        previous = self.store.replay(session_id, prompt, options, request_key)
        if previous:
            run = Run(self, previous)
            run.replayed = True
            return run
        session=self.get_session(session_id)
        if session.get('evaluation') and (execution is None or
                not isinstance(execution.get('context_package'), dict) or
                execution['context_package'].get('execution_mode') != 'evaluation_inputs_only'):
            raise BridgeError('evaluation_context_required',
                              'Evaluation instances require evaluation_inputs_only context.')
        if session.get('state') == 'archived':
            raise BridgeError('instance_archived', 'Archived instances cannot accept new messages.')
        from .routing.execution import admit_turn
        id,created,receipt,secrets = admit_turn(self, session, prompt, options, request_key, message_id)
        if not created:
            run = Run(self, id)
            run.replayed = True
            return run
        try:
            ContractRegistry(self.store).record_run(id, receipt)
        except Exception:
            self.store.finish(id,'failed','launch_failed')
            raise BridgeError('launch_failed', 'Could not persist provider compatibility before execution.') from None
        from .run_launcher import launch
        self._children[id] = launch(self.store, id, secrets, execution=execution)
        run = Run(self, id)
        run.replayed = False
        return run

    def export_context(self, session_id, *, budget_bytes=128000):
        self.get_session(session_id)
        return build(self.store,session_id,budget_bytes)

    def instance_export(self, instance_id, *, budget_bytes=128000, include_events=True,
                        include_unknowns=True):
        if not include_events or not include_unknowns:
            raise UnsupportedError('Selective context export is not supported by this adapter.')
        return self.export_context(instance_id, budget_bytes=budget_bytes)

    def quota(self, account_id, *, allow_network=False, oauth_env=None):
        raise UnsupportedError('Read observed proxy usage with accounts.usage.')

    def recover(self, instance_id=None, turn_id=None):
        """Reattach live workers. Never repeat an interrupted request automatically."""
        report={'live':[],'interrupted':[],'unresolved':[]}
        for row in self.runs():
            if instance_id and row['session_id'] != instance_id:
                continue
            if turn_id and row['id'] != turn_id:
                continue
            if row['state'] in TERMINAL:continue
            id=row['id']
            if alive(row['worker_pid'],row['worker_identity']):report['live'].append(id);continue
            if row['state']=='starting' and time.time()-row['created']<15:
                report['unresolved'].append(id);continue
            if alive(row['child_pid'],row['child_identity']):
                report['unresolved'].append(id);continue # Never rerun work while its process may still execute.
            self.store.emit(id,'recovery',{'reason':'worker_lost','automatic_retry':False})
            pending=unresolved(self.run(id)._observations())
            for item in pending:
                kind='subagent' if item.get('kind')=='subagent' else 'tool_result'
                self.store.emit(id,kind,{**item,'outcome':'unknown','status':'unknown','reason':'worker_lost'})
            self.store.finish(id,'interrupted','worker_lost')
            report['interrupted'].append(id)
        return report

    def close(self, *, cancel=False):
        """Default detaches; cancel=True waits for runs owned by this Bridge instance."""
        for id,proc in list(self._children.items()):
            if proc.poll() is not None:
                proc.wait();self._children.pop(id,None);continue
            if cancel and self.run(id).status not in TERMINAL:self.run(id).stop(wait=True)
            if proc.poll() is not None:proc.wait();self._children.pop(id,None)

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
