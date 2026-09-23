"""Constrain Codex skill discovery to one selected context package."""

from .errors import BridgeError


def fail(code, message):
    raise BridgeError(code, message, phase='launch', outcome='not_started')


def selected_config(listing, selected, cwd):
    data = listing.get('data')
    if not isinstance(data, list) or len(data) != 1:
        fail('skill_discovery_failed', 'Codex skill discovery did not return one workspace.')
    workspace = data[0]
    if (not isinstance(workspace, dict) or workspace.get('cwd') != cwd
            or workspace.get('errors')
            or not isinstance(workspace.get('skills'), list)
            or len(workspace['skills']) > 512):
        fail('skill_discovery_failed', 'Codex skill discovery is incomplete.')
    discovered = {}
    for item in workspace['skills']:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or not isinstance(item.get('name'), str)
                or item['path'] in discovered):
            fail('skill_discovery_failed', 'Codex returned an invalid skill list.')
        discovered[item['path']] = item['name']
    selected_paths = {item['path'] for item in selected}
    if len(selected_paths) != len(selected) or not selected_paths.issubset(discovered):
        fail('skill_unavailable', 'A selected skill is unavailable to Codex.')
    for item in selected:
        item['name'] = discovered[item['path']]
    return [{'path': path, 'enabled': path in selected_paths} for path in discovered]
