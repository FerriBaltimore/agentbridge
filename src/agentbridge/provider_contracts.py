"""Reviewed release bindings, immutable dialects and durable drift observations.

A release is not a contract. Multiple exact releases can share one dialect;
inspection suggests reuse but never grants support or changes active bindings.
"""
from hashlib import sha256
from importlib.resources import files
import json
import time

from .errors import BridgeError
from .models import ENGINES
from .store import dumps


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def index():
    try:
        return _index()
    except (OSError, ValueError, KeyError, TypeError):
        raise BridgeError('provider_contract_invalid', 'The installed contract index is unreadable or invalid.') from None


def _index():
    value = json.loads(files('agentbridge').joinpath('provider_contract_index.json').read_text())
    profiles = {}
    for profile in value['contracts']:
        expected = 'sha256:' + digest(profile['manifest'])
        if profile['id'] != expected or expected in profiles:
            raise BridgeError('provider_contract_invalid', 'The installed contract index is corrupt.')
        profiles[expected] = profile['manifest']['engine']
    releases = set()
    for binding in value['bindings']:
        release = (binding['engine'], binding['version'])
        component = binding['engine'] + '-cli'
        if (release in releases or profiles.get(binding['contract_id']) != binding['engine']
                or binding['engine'] not in ENGINES or binding['component'] != component):
            raise BridgeError('provider_contract_invalid', 'The installed release bindings are inconsistent.')
        releases.add(release)
    return value


class ContractRegistry:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS provider_inspections(
                    id TEXT PRIMARY KEY, engine TEXT NOT NULL, version TEXT,
                    created REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_contracts(
                    run_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            ''')

    def list(self, engine=None):
        if engine is not None and engine not in ENGINES:
            raise BridgeError('invalid_engine', 'Unknown engine.')
        data = index()
        return {**data, 'contracts': [p for p in data['contracts']
                                    if engine is None or p['manifest']['engine'] == engine],
                'bindings': [b for b in data['bindings'] if engine is None or b['engine'] == engine]}

    def get(self, contract_id):
        for profile in index()['contracts']:
            if profile['id'] == contract_id:
                return profile
        raise BridgeError('not_found', 'Provider contract not found.')

    def check(self, account, *, enforce=False):
        from .error_observer import provider_version
        version = provider_version(account, self.store.root)
        binding = next((b for b in index()['bindings']
                        if b['engine'] == account.engine and b['version'] == version), None)
        state = 'custom_adapter' if account.command else 'version_unavailable' if version is None else (
            'reviewed' if binding else 'unindexed_version')
        inspection = self.latest(account.engine, version, decisive=True) if version else None
        if binding and inspection and inspection['status'] == 'drift_detected':
            state = 'drift_detected'
        value = {'engine': account.engine, 'component': account.engine + '-cli',
                 'version': version, 'contract_id': binding['contract_id'] if binding else None,
                 'status': state, 'native_operations_allowed': state in {'reviewed', 'custom_adapter'},
                 'verification': 'reviewed_binding' if state == 'reviewed' else 'unverified',
                 'inspection_id': inspection['inspection_id'] if inspection else None,
                 'limitations': ['version_binding_is_not_live_acceptance', 'server_behavior_can_change_without_cli_upgrade']}
        if account.command:
            value['limitations'].append('custom_launcher_is_host_managed_and_outside_release_index')
        if enforce and not value['native_operations_allowed']:
            raise BridgeError('provider_contract_unverified',
                              'Inspect and review this provider release before starting native operations.',
                              details={'compatibility': value})
        return value

    def record_run(self, run_id, receipt):
        value = {**receipt, 'recorded_at': time.time()}
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO run_contracts(run_id,data) VALUES (?,?)', (run_id, dumps(value)))
        return self.run(run_id)

    def run(self, run_id):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM run_contracts WHERE run_id=?', (run_id,)).fetchone()
        return json.loads(row['data']) if row else None

    def verify_run(self, account, run_id):
        current = self.check(account, enforce=True)
        saved = self.run(run_id)
        if saved and any(saved.get(key) != current.get(key) for key in ('version', 'contract_id', 'status')):
            raise BridgeError('provider_contract_changed',
                              'The provider changed between admission and execution.', phase='launch')
        if saved is None:
            # Pre-registry admitted records remain inspectable and are checked
            # before execution; no previous version evidence is invented.
            self.record_run(run_id, {**current, 'admission_version_unobserved': True})
        return current

    def latest(self, engine, version, *, decisive=False):
        clause = "AND json_extract(data,'$.status') IN ('unchanged','drift_detected') " if decisive else ''
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM provider_inspections WHERE engine=? AND version=? '
                             + clause + 'ORDER BY created DESC, rowid DESC LIMIT 1', (engine, version)).fetchone()
        return json.loads(row['data']) if row else None

    def inspect(self, engine):
        from .provider_fingerprint import inspect_surface
        if engine not in ENGINES:
            raise BridgeError('invalid_engine', 'Unknown engine.')
        try:
            observed = inspect_surface(engine, state_root=self.store.root)
        except BridgeError as error:
            if error.code == 'provider_contract_changed' and error.details.get('engine') == engine:
                observed = {**error.details, 'structural_hash': None, 'evidence_kind': None,
                            'surface_names': [], 'limitations': ['provider_components_changed']}
            else:
                raise
        bindings = self.list(engine)['bindings']
        exact = next((b for b in bindings if b['version'] == observed.get('version')), None)
        matching = [b for b in bindings if b.get('structural_hash') and
                    b['structural_hash'] == observed.get('structural_hash') and
                    b.get('evidence_kind') == observed.get('evidence_kind')]
        status = 'candidate'
        if exact and exact.get('structural_hash') and observed.get('structural_hash'):
            status = 'unchanged' if exact in matching else 'drift_detected'
        elif exact:
            status = 'insufficient_evidence'
        reusable = matching[0]['contract_id'] if matching and not observed.get('evidence_kind', '').startswith('cli_help_observation') else None
        if 'bundled_sdk_version_mismatch' in observed.get('limitations', []):
            status, reusable = 'drift_detected', None
        if 'provider_components_changed' in observed.get('limitations', []):
            status, reusable = 'drift_detected', None
        value = {**observed, 'engine': engine, 'status': status,
                 'suggested_contract_id': reusable, 'activation': 'requires_reviewed_release_binding',
                 'schema_equality_is_not_behavioral_equivalence': True}
        inspection_id = digest(value)
        value['inspection_id'] = inspection_id
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO provider_inspections(id,engine,version,created,data) VALUES (?,?,?,?,?)',
                       (inspection_id, engine, observed.get('version'), time.time(), dumps(value)))
        return value
