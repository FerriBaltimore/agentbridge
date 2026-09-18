import pytest
from agentbridge import Account, BridgeError, RunOptions

def test_account_never_accepts_credential_value_and_normalizes_home(tmp_path):
    account=Account('codex-main','codex',home=str(tmp_path),env_names=('OPENAI_API_KEY',))
    assert account.home==str(tmp_path.resolve())
    assert 'OPENAI_API_KEY' in account.env_names
    with pytest.raises(BridgeError): Account('x','codex',home=str(tmp_path),env_names=('secret-value',))

def test_options_are_bounded():
    with pytest.raises(BridgeError):RunOptions(timeout=0)
    with pytest.raises(BridgeError):RunOptions(sandbox='anything')
    with pytest.raises(BridgeError):RunOptions(max_budget_usd=0)
