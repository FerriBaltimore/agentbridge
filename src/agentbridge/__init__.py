"""AgentBridge: independent execution, continuity and observation of coding agents."""
from .client import Bridge, Run
from .accounts import AccountService
from .continuity import ContextBundle
from .errors import BridgeError, BusyError, UnsupportedError
from .models import Account, Capabilities, Event, RunOptions
from .grantbridge import GrantBridgeClient

__version__='0.1.0'
__all__=['Bridge','Run','AccountService','GrantBridgeClient','Account','RunOptions','Event','Capabilities','ContextBundle','BridgeError','BusyError','UnsupportedError']
