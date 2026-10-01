"""Private execution inputs and the public MCP descriptor boundary."""
from pathlib import Path, PurePosixPath
import re
import sys
from uuid import UUID
from urllib.parse import urlsplit

from .context_package import digest, validate as validate_context
from .errors import BridgeError


MCP_SOCKET_ENV = 'AGENTBRIDGE_MCP_SOCKET_PATH'
MCP_OPERATION_ENV = 'AGENTBRIDGE_MCP_OPERATION_ID'
MCP_CAPABILITY_ENV = 'AGENTBRIDGE_MCP_CAPABILITY'
PRIVATE_EXECUTION_KEY = '_agentbridge_private_execution_v1'


def invalid_mcp():
    raise BridgeError('invalid_mcp', 'The MCP execution descriptor is invalid.')


def validate_mcp(value):
    if not isinstance(value, dict) or type(value.get('version')) is not int:
        invalid_mcp()
    version = value['version']
    endpoint = 'socket_path' if version == 1 else 'servers'
    if version not in {1, 2} or set(value) != {
            'version', endpoint, 'operation_id', 'capability'}:
        invalid_mcp()
    if version == 1:
        path = value['socket_path']
        if (not isinstance(path, str) or not 1 <= len(path) <= 4096
                or '\0' in path or '\\' in path or not PurePosixPath(path).is_absolute()
                or str(PurePosixPath(path)) != path or '..' in PurePosixPath(path).parts):
            invalid_mcp()
    else:
        validate_http_servers(value['servers'])
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


def validate_http_servers(servers):
    if not isinstance(servers, list) or not 1 <= len(servers) <= 100:
        invalid_mcp()
    names = set()
    for server in servers:
        if (not isinstance(server, dict) or set(server) != {'name', 'url'}
                or not isinstance(server['name'], str)
                or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', server['name']) is None
                or server['name'] in names or not isinstance(server['url'], str)
                or not 1 <= len(server['url']) <= 2048):
            invalid_mcp()
        names.add(server['name'])
        try:
            url = urlsplit(server['url'])
            if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
                    or url.username or url.password or url.query or url.fragment
                    or re.fullmatch(r'/[A-Za-z0-9_/-]+', url.path) is None
                    or server['url'] != f'http://127.0.0.1:{url.port}{url.path}'):
                invalid_mcp()
        except ValueError:
            invalid_mcp()


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
    binding_digest = digest({key: value for key, value in mcp.items()
                             if key != 'capability'}) if mcp is not None else None
    execution = {'context_package': context_package, 'mcp': mcp}
    return execution, package_digest, binding_digest


def mcp_endpoint_digest(mcp):
    return digest({key: value for key, value in mcp.items()
                   if key not in {'operation_id', 'capability'}}) if mcp else None


def verify(options, execution):
    validate_access(options)
    if not isinstance(execution, dict) or set(execution) != {'context_package', 'mcp'}:
        raise BridgeError('context_required', 'Fresh execution context is required for this turn.')
    prepared, package_digest, binding_digest = prepare(
        execution['context_package'], execution['mcp'])
    if (options.context_package_digest != package_digest
            or options.mcp_binding_digest != binding_digest):
        raise BridgeError('context_mismatch', 'Execution context does not match the admitted turn.')
    if (options.mcp_endpoint_digest is not None
            and options.mcp_endpoint_digest != mcp_endpoint_digest(prepared['mcp'])):
        raise BridgeError('context_mismatch', 'MCP endpoint differs from its admitted boundary.')
    package = prepared['context_package']
    if (package and (package.get('read_only_paths') or 'workspace_write' in package)
            and not options.host_isolated):
        raise BridgeError('invalid_execution_policy',
                          'Read-only projections require host-isolated execution.')
    if (prepared['mcp'] and prepared['mcp']['version'] == 2 and not options.host_isolated):
        raise BridgeError('invalid_execution_policy',
                          'Local HTTP MCP requires host-isolated execution.')
    if (options.host_isolated and package is not None
            and package.get('execution_mode', 'normal') != 'normal'):
        raise BridgeError('invalid_execution_policy',
                          'Inputs-only execution cannot enable native workspace tools.')
    return prepared


def validate_access(options):
    """Selected host inputs keep their isolation boundary in every transport."""
    if options.host_isolated and options.sandbox != 'danger-full-access':
        raise BridgeError('invalid_execution_policy',
                          'Host-isolated native tools require explicit full access.')
    if (options.sandbox == 'danger-full-access' and not options.host_isolated
            and (options.context_package_digest or options.mcp_binding_digest)):
        raise BridgeError('invalid_execution_policy',
                          'Full access cannot be combined with selected context or MCP isolation. '
                          'Use a restricted sandbox for selected execution inputs.')


def mcp_environment(descriptor):
    if descriptor is None:
        return {}
    return {**({MCP_SOCKET_ENV: descriptor['socket_path']} if descriptor['version'] == 1 else {}),
            MCP_OPERATION_ENV: descriptor['operation_id'],
            MCP_CAPABILITY_ENV: descriptor['capability']}


def codex_config(*, mcp_enabled=False, execution_mode='normal', selected_context=False,
                 host_isolated=False, mcp=None):
    config = {}
    if selected_context:
        config['project_doc_max_bytes'] = 0
        config['web_search'] = 'disabled'
    if mcp_enabled:
        config['mcp_servers'] = {'agentbridge_execution': {
            'command': sys.executable,
            'args': ['-P', '-m', 'agentbridge.mcp_bridge'],
            'env': {'PYTHONPATH': str(Path(__file__).resolve().parents[1])},
            'env_vars': [MCP_SOCKET_ENV, MCP_OPERATION_ENV, MCP_CAPABILITY_ENV],
            'required': True,
            'startup_timeout_sec': 10,
            'tool_timeout_sec': 25,
        }}
    if mcp is not None and mcp['version'] == 2:
        validate_mcp(mcp)
        config['mcp_servers'] = {server['name']: {
            'url': server['url'],
            'http_headers': {'Authorization': 'Bearer ' + mcp['capability']},
            'default_tools_approval_mode': 'approve', 'required': True,
            'startup_timeout_sec': 10, 'tool_timeout_sec': 25,
        } for server in mcp['servers']}
    if selected_context:
        config['features'] = {'apps': False, 'multi_agent': False,
                              'skill_mcp_dependency_install': False}
    if (not host_isolated and (mcp_enabled or selected_context)) or execution_mode != 'normal':
        config.setdefault('features', {}).update({'shell_tool': False, 'unified_exec': False})
    return config
