"""Selected context fails closed when native skill isolation is uncertain."""
import pytest

from agentbridge.codex_skills import selected_config
from agentbridge.errors import BridgeError


def test_selected_skills_disable_every_unselected_native_entry():
    selected = [{'type': 'skill', 'name': 'requested', 'path': '/selected/SKILL.md'}]
    listing = {'data': [{'cwd': '/workspace', 'skills': [
        {'path': '/selected/SKILL.md', 'name': 'native-selected'},
        {'path': '/other/SKILL.md', 'name': 'other'}], 'errors': []}]}
    assert selected_config(listing, selected, '/workspace') == [
        {'path': '/selected/SKILL.md', 'enabled': True},
        {'path': '/other/SKILL.md', 'enabled': False}]
    assert selected[0]['name'] == 'native-selected'


@pytest.mark.parametrize('listing,code', [
    ({'data': []}, 'skill_discovery_failed'),
    ({'data': [{'cwd': '/another-workspace', 'skills': []}]}, 'skill_discovery_failed'),
    ({'data': [{'cwd': '/workspace', 'skills': [], 'errors': ['failed']}]}, 'skill_discovery_failed'),
    ({'data': [{'cwd': '/workspace', 'skills': [{'path': '/other/SKILL.md', 'name': 'other'}]}]},
     'skill_unavailable'),
    ({'data': [{'cwd': '/workspace', 'skills': [
        {'path': '/selected/SKILL.md', 'name': 'selected'},
        {'path': '/selected/SKILL.md', 'name': 'duplicate'}]}]},
     'skill_discovery_failed'),
])
def test_invalid_skill_discovery_fails_before_native_thread(listing, code):
    with pytest.raises(BridgeError) as error:
        selected_config(listing, [{'type': 'skill', 'name': 'selected',
                                   'path': '/selected/SKILL.md'}], '/workspace')
    assert error.value.code == code
