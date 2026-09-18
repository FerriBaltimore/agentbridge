import pytest
from agentbridge.errors import BridgeError
from agentbridge.native import locate,safe_path

def test_native_paths_reject_escape_and_links(tmp_path):
    with pytest.raises(BridgeError):safe_path(tmp_path,'../outside')
    (tmp_path/'sessions').mkdir()
    with pytest.raises(BridgeError):locate('codex',tmp_path,'../../etc/passwd')
