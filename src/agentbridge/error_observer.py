"""Connect execution errors to durable cases without initiating diagnosis or recovery."""
import importlib.metadata
import re

from .catalog_process import read_output
from .error_evidence import from_issue
from .error_learning import Learning
from .errors import BridgeError
from .provider_contracts import ContractRegistry
from .security import base_environment


def provider_version(account):
    if account.command:
        return None  # Custom launchers have no guaranteed version-query contract.
    try:
        if account.engine == 'cursor':
            version = importlib.metadata.version('cursor-sdk')
        else:
            env = base_environment()
            version = read_output([account.engine, '--version'], env=env,
                                  timeout=2, max_bytes=256).decode('utf-8')
        # Version labels are a compatibility boundary, not free-text search.
        # Never assign prerelease/build/custom output to the stable version's rules.
        number = r'([0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5})'
        pattern = {'codex': r'codex-cli ' + number,
                   'claude': number + r' \(Claude Code\)', 'cursor': number}[account.engine]
        match = re.fullmatch(pattern, version.strip())
        return match.group(1) if match else None
    except (BridgeError, OSError, UnicodeDecodeError, importlib.metadata.PackageNotFoundError):
        return None


def run_version(store, account, turn_id):
    """An admitted release remains the source for this turn after an upgrade."""
    receipt = ContractRegistry(store).run(turn_id)
    if receipt is None:
        return provider_version(account)  # Older runs have no admission receipt.
    version = receipt.get('version') if receipt.get('engine') == account.engine else None
    return version if isinstance(version, str) and re.fullmatch(
        r'[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}', version) else None


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
            self.version = run_version(self.store, self.account, self.turn_id)
        fingerprint = evidence['fingerprint']
        if fingerprint not in self.seen:
            self.seen[fingerprint] = self.learning.capture(self.account.engine, evidence,
                turn_id=self.turn_id, provider_version=self.version)['id']
        issue = {**issue, 'details': {**issue['details'], 'case_id': self.seen[fingerprint]}}
        return self.learning.apply(self.account.engine, evidence, issue, provider_version=self.version)
