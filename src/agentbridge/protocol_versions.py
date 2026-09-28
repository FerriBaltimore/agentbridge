"""Protocol numbers a consumer compares before starting this AgentBridge.

`tools/release_manifest.py` copies these values into `dist/agentbridge-<version>.manifest.json`
so Fullbrain v2 can reject an AgentBridge/GrantBridge pair before a worker starts. The rules
and the history of every number are in docs/compatibility.md. Change a number only in the
commit that changes the shape it names, and record it in CHANGELOG.md.
"""

# `capabilities.get` contract identifier over JSON-RPC stdio; Fullbrain v2 compares it exactly.
RPC_CONTRACT = 'v2'
# Playground HTTP API: `/api/meta` `api_revision` and the `X-AgentBridge-API-Revision` header.
HTTP_API_REVISION = 2
# Command line a host invokes: `agentbridge --root DIR rpc`, `--version`, `accounts login-*`.
CLI = 1
# `accounts.login.start` shape. 1: same_host only (2.0.0). 2: `browser`
# same_host|isolated|mobile, `mode` browser|hosted, `viewer_url`, attempt projections echo
# `browser` and `mode`, `identity.email` on `identity_changed` (2.5.0).
LOGIN_START = 2
# Oldest GrantBridge release whose engine surface (`auth.proxy_*`) serves every login entry
# when AgentBridge is pointed at an external GrantBridge instead of its bundled adapter.
GRANTBRIDGE_MIN = '1.0.0-rc.1'


def protocols():
    """The `protocols` object of the release manifest."""
    return {'rpc_contract': RPC_CONTRACT, 'http': HTTP_API_REVISION, 'cli': CLI,
            'login_start': LOGIN_START}
