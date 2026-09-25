"""Public conversation input validation and private execution submission."""

from .errors import BridgeError, UnsupportedError
from .execution_context import prepare
from .models import RunOptions


class MessageSubmissionMixin:
    def message_create(self, instance_id, content, *, model=None, effort=None, context_window=None,
                       permission_mode='dontAsk', sandbox_mode='read-only', allowed_tools=(),
                       max_turns=None, max_budget=None, timeout_ms=None, attachments=None,
                       provider_options=None, metadata=None, idempotency_key=None,
                       context_package=None, mcp=None, excluded_account_refs=(),
                       delivery='reject', position=None, expected_version=None, expected_turn_id=None):
        if delivery not in ('reject', 'queue', 'steer', 'interrupt'):
            raise BridgeError('invalid_delivery', 'Choose reject, queue, steer or interrupt delivery.')
        if delivery == 'reject' and any(value is not None for value in (
                position, expected_version, expected_turn_id)):
            raise BridgeError('invalid_request', 'Queue controls require explicit queue delivery.')
        prompt = self._message_text(content)
        if provider_options or metadata:
            raise UnsupportedError('Provider-specific options and metadata require an adapter contract.')
        execution, package_digest, binding_digest = prepare(context_package, mcp)
        options = RunOptions(
            timeout=(timeout_ms / 1000) if timeout_ms is not None else 600,
            sandbox=sandbox_mode, permission_mode=permission_mode,
            allowed_tools=tuple(allowed_tools), model=model,
            context_window=context_window, effort=effort, max_turns=max_turns,
            max_budget_usd=max_budget, collect_usage=True, attachments=attachments,
            context_package_digest=package_digest, mcp_binding_digest=binding_digest,
        )
        if delivery != 'reject':
            return self._queue_submit(
                instance_id, prompt, options,
                execution=execution if package_digest or binding_digest else None,
                exclusions=excluded_account_refs, idempotency_key=idempotency_key,
                position=position, expected_version=expected_version,
                delivery=delivery, expected_turn_id=expected_turn_id)
        run = self.submit(instance_id, prompt, options=options, request_key=idempotency_key,
                          execution=execution if package_digest or binding_digest else None,
                          excluded_account_refs=excluded_account_refs)
        message_id = run.snapshot.get('message_id', run.id)
        return {'turn_id': run.id, 'message_id': message_id, 'instance_id': instance_id,
                'state': run.status, 'replayed': bool(getattr(run, 'replayed', False)),
                'account_ref': self.account_reference(run.snapshot['account_id'])}

    @staticmethod
    def _message_text(content):
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list) and all(isinstance(item, dict) and item.get('type') == 'text' for item in content):
            value = ''.join(item.get('text', '') for item in content)
            if value.strip():
                return value
        raise BridgeError('invalid_request', 'content must contain nonempty text.')
