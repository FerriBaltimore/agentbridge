"""Codex proxy duplex transport inside the supervised process group."""
import json
import os
import sys
from contextlib import nullcontext
from tempfile import TemporaryDirectory

from .codex_control import CodexControl
from .context_package import materialize
from .execution_context import codex_config
from .errors import BridgeError
from .permissions import Permissions
from .provider_channel import ProviderChannel
from .native_sandbox import available as isolation_available
from .security import Redactor
from .store import Store


def main():
    emit = lambda value: print(json.dumps(value), flush=True)
    def approve(details, deliver):
        if payload['options']['permission_mode'] == 'dontAsk':
            deliver('deny')
            return
        permission_id = broker.request(payload['turn_id'], redactor.clean(details),
                                       timeout=payload['options']['timeout'])
        decision = broker.wait(payload['turn_id'], permission_id)
        deliver(decision)
        broker.delivered(payload['turn_id'], permission_id, decision)
    try:
        payload = json.load(sys.stdin)
        if payload.get('engine') != 'codex':
            raise BridgeError('invalid_proxy_account', 'Interactive execution requires Codex.')
        broker = Permissions(Store(payload['root']))
        redactor = Redactor(os.environ.get(key, '') for key in payload['secret_names'])
        package = payload.get('context_package')
        inputs_only = (package is not None
                       and package.get('execution_mode', 'normal') != 'normal')
        if inputs_only and not isolation_available():
            raise BridgeError('isolation_unavailable',
                              'Inputs-only filesystem isolation is unavailable.',
                              phase='launch', outcome='not_started')
        context = materialize(os.environ['CODEX_HOME'], package) if package else nullcontext((None, [], []))
        with context as (developer, skills, evidence):
            payload['developer_instructions'] = developer
            payload['skill_inputs'] = skills
            payload['evidence_inputs'] = evidence
            payload['codex_config'] = codex_config(
                mcp_enabled=payload.get('mcp_enabled') is True,
                execution_mode=package.get('execution_mode', 'normal') if package else 'normal',
                selected_context=package is not None)
            temporary = TemporaryDirectory(prefix='agentbridge-inputs-') if inputs_only else nullcontext(None)
            with temporary as temp_dir:
                native_env = os.environ.copy()
                if inputs_only:
                    native_env['TMPDIR'] = temp_dir
                    native_env['HOME'] = native_env['CODEX_HOME']
                with ProviderChannel(payload['command'], cwd=payload['cwd'], env=native_env,
                                     inputs_only=inputs_only) as channel:
                    CodexControl(channel, payload, emit, approve).execute()
    except BridgeError as error:
        emit({'type': 'bridge_error', 'error': error.safe_data(),
              'outcome': 'not_started' if error.phase == 'launch' else 'unknown'})
        raise SystemExit(1) from None
    except Exception:
        # Persistence and wrapper exceptions may include sensitive native data.
        # Keep the parent worker informed without printing a Python traceback.
        emit({'type': 'bridge_error', 'error': {'code': 'worker_failed'}, 'outcome': 'unknown'})
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
