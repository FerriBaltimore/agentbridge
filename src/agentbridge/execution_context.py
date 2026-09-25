"""Private execution inputs and the public MCP descriptor boundary."""
from pathlib import PurePosixPath
import re
import sys
from uuid import UUID

from .context_package import digest, validate as validate_context
from .errors import BridgeError


MCP_SOCKET_ENV = 'AGENTBRIDGE_MCP_SOCKET_PATH'
MCP_OPERATION_ENV = 'AGENTBRIDGE_MCP_OPERATION_ID'
MCP_CAPABILITY_ENV = 'AGENTBRIDGE_MCP_CAPABILITY'
PRIVATE_EXECUTION_KEY = '_agentbridge_private_execution_v1'


def invalid_mcp():
    raise BridgeError('invalid_mcp', 'The MCP execution descriptor is invalid.')


def validate_mcp(value):
    if not isinstance(value, dict) or set(value) != {
            'version', 'socket_path', 'operation_id', 'capability'}:
        invalid_mcp()
    if type(value['version']) is not int or value['version'] != 1:
        invalid_mcp()
    path = value['socket_path']
    if (not isinstance(path, str) or not 1 <= len(path) <= 4096
            or '\0' in path or '\\' in path or not PurePosixPath(path).is_absolute()
            or str(PurePosixPath(path)) != path or '..' in PurePosixPath(path).parts):
        invalid_mcp()
    operation = value['operation_id']
    try:
        if not isinstance(operation, str) or str(UUID(operation)) != operation:
            invalid_mcp()
    except (ValueError, AttributeError):
        invalid_mcp()
    capability = value['capability']
    if (not isinstance(capability, str) or not 16 <= len(capability) <= 256
            or re.fullmatch(r'[A-Za-z0-9_-]+', capability) is None):
        invalid_mcp()
    return value


def prepare(context_package, mcp):
    if context_package is not None:
        validate_context(context_package)
        if context_package.get('execution_mode', 'normal') != 'normal' and mcp is not None:
            raise BridgeError('invalid_context', 'Inputs-only context cannot use MCP tools.')
        if context_package.get('tools') and mcp is None:
            raise BridgeError('mcp_required', 'Selected tools require an MCP execution descriptor.')
    if mcp is not None:
        validate_mcp(mcp)
    package_digest = digest(context_package) if context_package is not None else None
    binding_digest = digest({key: mcp[key] for key in (
        'version', 'socket_path', 'operation_id')}) if mcp is not None else None
    execution = {'context_package': context_package, 'mcp': mcp}
    return execution, package_digest, binding_digest


def verify(options, execution):
    validate_access(options)
    if not isinstance(execution, dict) or set(execution) != {'context_package', 'mcp'}:
        raise BridgeError('context_required', 'Fresh execution context is required for this turn.')
    prepared, package_digest, binding_digest = prepare(
        execution['context_package'], execution['mcp'])
    if (options.context_package_digest != package_digest
            or options.mcp_binding_digest != binding_digest):
        raise BridgeError('context_mismatch', 'Execution context does not match the admitted turn.')
    return prepared


def validate_access(options):
    """Selected host inputs keep their isolation boundary in every transport."""
    if (options.sandbox == 'danger-full-access'
            and (options.context_package_digest or options.mcp_binding_digest)):
        raise BridgeError('invalid_execution_policy',
                          'Full access cannot be combined with selected context or MCP isolation. '
                          'Use a restricted sandbox for selected execution inputs.')


def mcp_environment(descriptor):
    if descriptor is None:
        return {}
    return {MCP_SOCKET_ENV: descriptor['socket_path'],
            MCP_OPERATION_ENV: descriptor['operation_id'],
            MCP_CAPABILITY_ENV: descriptor['capability']}


def codex_config(*, mcp_enabled=False, execution_mode='normal', selected_context=False):
    config = {}
    if selected_context:
        config['project_doc_max_bytes'] = 0
        config['web_search'] = 'disabled'
    if mcp_enabled:
        config['mcp_servers'] = {'agentbridge_execution': {
            'command': sys.executable,
            'args': ['-P', '-m', 'agentbridge.mcp_bridge'],
            'env_vars': ['PYTHONPATH', MCP_SOCKET_ENV, MCP_OPERATION_ENV, MCP_CAPABILITY_ENV],
            'required': True,
            'startup_timeout_sec': 10,
            'tool_timeout_sec': 25,
        }}
    if selected_context:
        config['features'] = {'apps': False, 'multi_agent': False,
                              'skill_mcp_dependency_install': False}
    if mcp_enabled or selected_context or execution_mode != 'normal':
        config.setdefault('features', {}).update({'shell_tool': False, 'unified_exec': False})
    return config
