"""Durable unknown-error cases and explicitly reviewed classification rules."""
import json
import time
from uuid import uuid4

from .error_learning_contract import classify, identity, proposal
from .error_evidence import validate_evidence
from .errors import BridgeError
from .models import identifier, page_values
from .store import dumps


class Learning:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS error_cases(
                    id TEXT PRIMARY KEY, engine TEXT NOT NULL, provider_version TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, evidence TEXT NOT NULL, count INTEGER NOT NULL,
                    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                    first_turn_id TEXT, last_turn_id TEXT,
                    UNIQUE(engine, provider_version, fingerprint));
                CREATE TABLE IF NOT EXISTS error_proposals(
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, status TEXT NOT NULL,
                    target_code TEXT, provenance TEXT NOT NULL, validation TEXT,
                    revision INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS error_rules(
                    id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL,
                    case_id TEXT NOT NULL, engine TEXT NOT NULL, provider_version TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, target_code TEXT NOT NULL,
                    state TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS active_error_rule
                    ON error_rules(engine, provider_version, fingerprint) WHERE state='active';
                CREATE TABLE IF NOT EXISTS error_learning_audit(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                    operation TEXT NOT NULL, object_id TEXT NOT NULL, revision INTEGER NOT NULL);
            ''')

    @staticmethod
    def _public(row):
        value = dict(row)
        for key in ('evidence', 'provenance', 'validation'):
            if value.get(key) is not None:
                value[key] = json.loads(value[key])
        if value.get('provider_version') == '':
            value['provider_version'] = None
        return value

    @staticmethod
    def _get(db, table, id):
        identifier(id)
        if table not in {'error_cases', 'error_proposals', 'error_rules'}:
            raise ValueError(table)
        row = db.execute(f'SELECT * FROM {table} WHERE id=?', (id,)).fetchone()
        if row is None:
            raise BridgeError('error_record_not_found', 'The error learning record does not exist.')
        return Learning._public(row)

    @staticmethod
    def _audit(db, operation, id, revision):
        db.execute('INSERT INTO error_learning_audit(at,operation,object_id,revision) VALUES (?,?,?,?)',
                   (time.time(), operation, id, revision))

    @staticmethod
    def _revision(value, expected):
        if type(expected) is not int or value['revision'] != expected:
            raise BridgeError('version_conflict', 'The error learning record changed since review.')

    def _turn(self, turn_id):
        if turn_id is not None:
            identifier(turn_id)
            self.store.get('runs', turn_id)
        return turn_id

    def capture(self, engine, evidence, turn_id=None, provider_version=None):
        identity(engine, provider_version)
        value = validate_evidence(evidence)
        self._turn(turn_id)
        now, id = time.time(), uuid4().hex
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT INTO error_cases VALUES (?,?,?,?,?,1,?,?,?,?)
                ON CONFLICT(engine,provider_version,fingerprint) DO UPDATE SET
                count=count+1,last_seen=excluded.last_seen,last_turn_id=excluded.last_turn_id''',
                (id, engine, provider_version or '', value['fingerprint'], dumps(value),
                 now, now, turn_id, turn_id))
            row = db.execute('''SELECT * FROM error_cases
                WHERE engine=? AND provider_version=? AND fingerprint=?''',
                (engine, provider_version or '', value['fingerprint'])).fetchone()
            return self._public(row)

    def list_cases(self, limit=100, cursor=None):
        limit, offset = page_values(limit, 0 if cursor is None else cursor)
        limit = min(limit, 1000)
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM error_cases ORDER BY first_seen,id LIMIT ? OFFSET ?',
                              (limit + 1, offset)).fetchall()
        return {'items': [self._public(row) for row in rows[:limit]],
                'next_cursor': offset + limit if len(rows) > limit else None}

    def get_case(self, case_id):
        with self.store.connect() as db:
            return self._get(db, 'error_cases', case_id)

    def get_proposal(self, proposal_id):
        with self.store.connect() as db:
            return self._get(db, 'error_proposals', proposal_id)

    def get_rule(self, rule_id):
        with self.store.connect() as db:
            return self._get(db, 'error_rules', rule_id)

    def propose(self, case_id, result, provenance=None):
        value = proposal(result)
        origin = {'source': 'manual'} if provenance is None else provenance
        if (not isinstance(origin, dict) or set(origin) - {'source', 'operation_id'}
                or origin.get('source') not in {'manual', 'ai_session', 'fixture'}):
            raise BridgeError('invalid_error_proposal', 'Use a known provenance source and optional operation_id.')
        operation_id = origin.get('operation_id')
        if operation_id is not None and (
            not isinstance(operation_id, str) or len(operation_id) != 32
            or any(character not in '0123456789abcdef' for character in operation_id)
        ):
            raise BridgeError('invalid_error_proposal', 'The diagnosis operation_id must be an opaque UUID.')
        if origin['source'] == 'ai_session' and operation_id is None:
            raise BridgeError('invalid_error_proposal', 'AI proposals require a diagnosis operation_id.')
        now, id = time.time(), uuid4().hex
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._get(db, 'error_cases', case_id)
            db.execute('INSERT INTO error_proposals VALUES (?,?,?,?,?,?,1,?,?)',
                       (id, case_id, value['status'], value['target_code'], dumps(origin), None, now, now))
            self._audit(db, 'proposed', id, 1)
            return self._get(db, 'error_proposals', id)

    @staticmethod
    def _matches(rule, engine, value, provider_version):
        return (rule['engine'] == engine and rule['provider_version'] == provider_version
                and rule['fingerprint'] == value['fingerprint'])

    def validate(self, proposal_id):
        from .provider_errors import normalize
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item = self._get(db, 'error_proposals', proposal_id)
            if item['status'] not in {'proposed', 'validated_fixture'}:
                raise BridgeError('error_proposal_not_ready', 'Only proposed classifications can be validated.')
            case = self._get(db, 'error_cases', item['case_id'])
            rule = {**case, 'id': 'fixture', 'revision': 1, 'state': 'active',
                    'target_code': item['target_code']}
            issue = normalize(case['engine'], {}, outcome='unknown')
            changed = classify(issue, rule)
            checks = [self._matches(rule, case['engine'], case['evidence'], case['provider_version']),
                      changed['code'] == item['target_code'], changed['outcome'] == 'unknown',
                      changed['retryable'] is False, changed['action'] == 'inspect']
            different = {**case['evidence'], 'fingerprint': ('0' if case['fingerprint'][0] != '0' else '1')
                         + case['fingerprint'][1:]}
            checks.append(not self._matches(rule, case['engine'], different, case['provider_version']))
            alternate_engine = 'claude' if case['engine'] != 'claude' else 'codex'
            checks.append(not self._matches(rule, alternate_engine, case['evidence'], case['provider_version']))
            checks.append(not self._matches(rule, case['engine'], case['evidence'],
                                           '0.0' if case['provider_version'] != '0.0' else None))
            for code in ('safety_blocked', 'quota_exhausted', 'authentication_required', 'unknown_outcome'):
                known = normalize(case['engine'], {'code': code}, outcome='unknown')
                checks.append(classify(known, rule) == known)
            if not all(checks):
                raise BridgeError('error_validation_failed', 'The classification did not preserve error invariants.')
            validation = {'status': 'passed', 'checks': len(checks), 'kind': 'structural_fixture',
                          'semantic_verification': False}
            revision = item['revision'] + 1
            db.execute('''UPDATE error_proposals SET status='validated_fixture',validation=?,
                revision=?,updated_at=? WHERE id=?''', (dumps(validation), revision, time.time(), proposal_id))
            self._audit(db, 'validated_fixture', proposal_id, revision)
            return self._get(db, 'error_proposals', proposal_id)

    def activate(self, proposal_id, expected_revision):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item = self._get(db, 'error_proposals', proposal_id)
            self._revision(item, expected_revision)
            if item['status'] != 'validated_fixture':
                raise BridgeError('error_proposal_not_ready', 'Validate and review the proposal before activation.')
            case = self._get(db, 'error_cases', item['case_id'])
            existing = db.execute('''SELECT id FROM error_rules WHERE engine=? AND provider_version=?
                AND fingerprint=? AND state='active' ''',
                (case['engine'], case['provider_version'] or '', case['fingerprint'])).fetchone()
            if existing:
                raise BridgeError('error_rule_conflict', 'An active classification already matches this case.')
            id, now = uuid4().hex, time.time()
            db.execute('INSERT INTO error_rules VALUES (?,?,?,?,?,?,?,?,1,?,?)',
                       (id, proposal_id, case['id'], case['engine'], case['provider_version'] or '',
                        case['fingerprint'], item['target_code'], 'active', now, now))
            db.execute("UPDATE error_proposals SET status='active',revision=revision+1,updated_at=? WHERE id=?",
                       (now, proposal_id))
            self._audit(db, 'proposal_activated', proposal_id, item['revision'] + 1)
            self._audit(db, 'activated', id, 1)
            return self._get(db, 'error_rules', id)

    def deactivate(self, rule_id, expected_revision):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item = self._get(db, 'error_rules', rule_id)
            self._revision(item, expected_revision)
            if item['state'] != 'active':
                return item
            revision = item['revision'] + 1
            db.execute("UPDATE error_rules SET state='inactive',revision=?,updated_at=? WHERE id=?",
                       (revision, time.time(), rule_id))
            self._audit(db, 'deactivated', rule_id, revision)
            return self._get(db, 'error_rules', rule_id)

    def apply(self, engine, evidence, issue, provider_version=None):
        identity(engine, provider_version)
        value = validate_evidence(evidence)
        if issue.get('details', {}).get('detection') != 'unclassified':
            return issue
        with self.store.connect() as db:
            row = db.execute('''SELECT * FROM error_rules WHERE engine=? AND provider_version=?
                AND fingerprint=? AND state='active' ''',
                (engine, provider_version or '', value['fingerprint'])).fetchone()
        return classify(issue, self._public(row)) if row is not None else issue
