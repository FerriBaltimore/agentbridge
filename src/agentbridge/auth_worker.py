"""Hold the provider login process independently of the calling CLI/RPC process."""
import os
import signal
import sys
import time

from .accounts import AccountService
from .authentication import AuthenticationService, TERMINAL
from .auth_runtime import AuthRuntime
from .errors import BridgeError
from .grantbridge import GrantBridgeClient
from .store import Store


def execute(store, attempt_id, token):
    runtime = AuthRuntime(store)
    if not runtime.claim(attempt_id, token):
        return
    job = runtime.get(attempt_id)
    row = store.get_auth_attempt(attempt_id)
    service = AuthenticationService(store, AccountService(store))
    stopped = [False]
    signal.signal(signal.SIGTERM, lambda *_: stopped.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stopped.__setitem__(0, True))
    try:
        if row['status'] in TERMINAL | {'bound', 'usable'}:
            return
        with GrantBridgeClient(**job['config'], timeout=160 if job['kind'] != 'start' else 30) as client:
            if job['kind'] != 'start':
                remote = client.check(row['grantbridge_id'], row['owner'], inference=job['kind'] == 'inference')
                service._save_remote(row, remote)
                return
            if job['cancel_requested']:
                store.update_auth_attempt(attempt_id, row['owner'], status='cancelled')
                return
            remote = client.start(owner=row['owner'], engine=row['engine'], mode=row['mode'],
                                  browser=row['browser'], request_key=attempt_id)
            row = store.update_auth_attempt(attempt_id, row['owner'], grantbridge_id=remote['id'],
                                            status=service._status(remote), data=remote)
            deadline = time.monotonic() + 600
            while row['status'] not in TERMINAL | {'authorized', 'verified', 'bound'}:
                job = runtime.get(attempt_id)
                if stopped[0] or job['cancel_requested'] or time.monotonic() >= deadline:
                    remote = client.cancel(row['grantbridge_id'], row['owner'])
                    service._save_remote(row, remote)
                    return
                time.sleep(0.2)
                row = service._save_remote(row, client.get(row['grantbridge_id'], row['owner']))
    except Exception as error:
        # Only machine codes cross the boundary, never subprocess error bodies.
        code = error.code if isinstance(error, BridgeError) else 'authentication_worker_failed'
        current = store.get_auth_attempt(attempt_id, row['owner'])
        if current['status'] not in TERMINAL | {'bound'}:
            status = 'interrupted' if job['kind'] == 'start' else 'failed'
            store.update_auth_attempt(attempt_id, row['owner'], status=status,
                                      data={**current['data'], 'checking': False,
                                            'verification': None, 'error': {'code': code}})
    finally:
        runtime.finish(attempt_id, token)


if __name__ == '__main__':
    os.umask(0o077)
    execute(Store(sys.argv[1]), sys.argv[2], sys.argv[3])
