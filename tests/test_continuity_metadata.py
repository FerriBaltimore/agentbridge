"""Unknown metadata never becomes evidence of completion or authorization."""
import asyncio
import math
from types import SimpleNamespace

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.attachments import normalize as attachments
from agentbridge.continuity import unresolved
from agentbridge.errors import BridgeError
from agentbridge.message_projection import retractions, text
from agentbridge.permissions import Permissions
from agentbridge.process import identity
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


def event(seq, kind, **data):
    return SimpleNamespace(seq=seq, kind=kind, run_id='turn', data=data)


@pytest.mark.parametrize('outcome', [None, 'future_outcome', 'unknown'])
def test_missing_or_future_tool_outcome_stays_unresolved(outcome):
    events = [event(1, 'tool_call', call_id='tool'), event(2, 'tool_result', call_id='tool', outcome=outcome)]
    result = unresolved(events)
    assert len(result) == 1 and result[0]['outcome'] == 'unknown'


@pytest.mark.parametrize('outcome', ['completed', 'failed'])
def test_observed_known_tool_result_resolves_matching_call(outcome):
    events = [event(1, 'tool_call', call_id='tool'), event(2, 'tool_result', call_id='tool', outcome=outcome)]
    assert unresolved(events) == []


def test_result_without_identity_cannot_close_an_unidentified_call():
    events = [event(1, 'tool_call', name='future_tool'), event(2, 'tool_result', outcome='completed')]
    assert len(unresolved(events)) == 2


def test_retraction_keeps_newer_unfinished_response_deltas():
    events = [event(1, 'assistant', text='old response', provider_message_id='old'),
              event(2, 'text_delta', text='new partial response'),
              event(3, 'model_changed', retracted_provider_message_ids=['old']),
              event(4, 'run_finished', state='interrupted')]
    result = text(events)
    assert result['text'] == 'new partial response' and result['incomplete']
    assert result['retracted_provider_message_ids'] == ['old']


def test_subagent_retraction_cannot_hide_parent_response():
    events = [event(1, 'assistant', text='parent response', provider_message_id='main'),
              event(2, 'assistant', parent_id='subagent', text='child', supersedes=['main']),
              event(3, 'model_changed', parent_id='subagent', retracted_provider_message_ids=['main'])]
    assert text(events)['text'] == 'parent response'
    assert retractions(events) == {}


@pytest.mark.parametrize('metadata', [None, 'single_id', {'future': 'shape'}, 3])
def test_unknown_retraction_shape_is_not_iterated_as_message_ids(metadata):
    events = [event(1, 'assistant', text='retained', provider_message_id='single_id', supersedes=metadata),
              event(2, 'model_changed', retracted_provider_message_ids=metadata)]
    result = text(events)
    assert result['text'] == 'retained' and result['retracted'] is False


@pytest.fixture
def stored(tmp_path):
    bridge = Bridge(tmp_path / 'store')
    register_verified_proxy_account(bridge.store, 'fixture', 8317)
    session_id, _ = bridge.store.add_session('fixture-session', 'fixture', str(tmp_path), 'fixture-model')
    turn, _ = bridge.store.admit('fixture-turn', session_id, 'request', RunOptions(), None)
    return bridge, session_id, turn


@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf])
def test_nonfinite_transcript_time_filter_is_rejected(stored, value):
    bridge, session, _ = stored
    with pytest.raises(BridgeError) as caught:
        bridge.messages(session, after=value)
    assert caught.value.code == 'invalid_params'


@pytest.mark.parametrize('value', [math.nan, math.inf, -1, True, '3'])
def test_nonfinite_or_invalid_waits_fail_before_following_events(stored, value):
    bridge, _, turn = stored
    with pytest.raises(BridgeError) as caught:
        list(bridge.run(turn).events(follow=True, timeout=value))
    assert caught.value.code == 'invalid_timeout'
    with pytest.raises(BridgeError):
        bridge.run(turn).wait(value)
    async def read():
        return [event async for event in bridge.run(turn).aevents(timeout=value)]
    with pytest.raises(BridgeError):
        asyncio.run(read())


@pytest.mark.parametrize('options', [{'limit': True}, {'after': True}, {'after': -1}, {'after': math.nan}])
def test_invalid_event_cursor_or_limit_is_not_silently_coerced(stored, options):
    bridge, _, turn = stored
    with pytest.raises(BridgeError) as caught:
        list(bridge.run(turn).events(**options))
    assert caught.value.code == 'invalid_pagination'


def test_provider_permission_metadata_cannot_override_broker_identity(stored):
    bridge, _, turn = stored
    permissions = Permissions(bridge.store)
    request = permissions.request(turn, {'permission_id': 'provider-id', 'expires_at': -1,
                                         'operation': 'fixture'}, timeout=30)
    emitted = list(bridge.run(turn).events())[-1]
    assert emitted.kind == 'permission_required'
    assert emitted.data['permission_id'] == request and emitted.data['expires_at'] > 0


def test_permission_delivery_is_idempotent_and_conflicting_outcomes_fail(stored):
    bridge, _, turn = stored
    permissions = Permissions(bridge.store)
    request = permissions.request(turn, {'operation': 'fixture'}, timeout=30)
    permissions.respond(turn, request, 'deny')
    permissions.delivered(turn, request, 'deny')
    before = len(list(bridge.run(turn).events()))
    permissions.delivered(turn, request, 'deny')
    assert len(list(bridge.run(turn).events())) == before
    with pytest.raises(BridgeError) as caught:
        permissions.delivered(turn, request, 'allow')
    assert caught.value.code == 'idempotency_conflict'


@pytest.mark.parametrize('timeout', [math.nan, math.inf, -1, True])
def test_permission_expiry_requires_valid_finite_metadata(stored, timeout):
    bridge, _, turn = stored
    permissions = Permissions(bridge.store)
    with pytest.raises(BridgeError) as caught:
        permissions.request(turn, {'operation': 'fixture'}, timeout=timeout)
    assert caught.value.code == 'invalid_timeout'


@pytest.mark.parametrize('name', ['delete\x7f', 'invalid\ud800'])
def test_attachment_names_reject_nonprintable_or_invalid_unicode(name):
    with pytest.raises(BridgeError) as caught:
        attachments([{'type': 'text', 'name': name, 'text': 'content'}])
    assert caught.value.code == 'invalid_attachment'


@pytest.mark.parametrize('pid', [True, '../self', '123', -1, 0, math.inf])
def test_process_identity_requires_a_positive_numeric_pid(pid):
    assert identity(pid) is None
