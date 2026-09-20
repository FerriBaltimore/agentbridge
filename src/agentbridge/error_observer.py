"""Connect execution errors to durable cases without initiating diagnosis or recovery."""
import importlib.metadata
import re
import subprocess

from .error_evidence import from_issue
from .error_learning import Learning
from .security import base_environment


def provider_version(account):
    if account.command:
        return None  # Custom launchers have no guaranteed version-query contract.
    try:
        if account.engine == 'cursor':
            version = importlib.metadata.version('cursor-sdk')
        else:
            env = base_environment()
            env['CODEX_HOME' if account.engine == 'codex' else 'CLAUDE_CONFIG_DIR'] = account.home
            result = subprocess.run([account.engine, '--version'], env=env, capture_output=True,
                                    timeout=2, text=True)
            if result.returncode:
                return None
            version = result.stdout[:256]
        match = re.search(r'\b([0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5})\b', version)
        return match.group(1) if match else None
    except (OSError, subprocess.TimeoutExpired, importlib.metadata.PackageNotFoundError):
        return None


class ErrorObserver:
    def __init__(self, store, account, turn_id):
        self.store, self.account, self.turn_id = store, account, turn_id
        self.learning, self.version, self.seen = None, None, {}

    def __call__(self, issue):
        evidence = from_issue(issue)
        if evidence is None or issue.get('details', {}).get('detection') != 'unclassified':
            return issue
        if self.learning is None:
            self.learning = Learning(self.store)
            self.version = provider_version(self.account)
        fingerprint = evidence['fingerprint']
        if fingerprint not in self.seen:
            self.seen[fingerprint] = self.learning.capture(self.account.engine, evidence,
                turn_id=self.turn_id, provider_version=self.version)['id']
        issue = {**issue, 'details': {**issue['details'], 'case_id': self.seen[fingerprint]}}
        return self.learning.apply(self.account.engine, evidence, issue, provider_version=self.version)
