"""Cache reports retain uncertainty and never sum overlapping observations."""

from agentbridge.models import Event
from agentbridge.usage import summarize


def observed(tokens, *, scope='turn', aggregation=None, seq=1):
    return Event(seq, 'turn', 'instance', 'usage', 1000, {
        'source': 'codex_exec', 'scope': scope,
        'aggregation': aggregation, 'tokens': tokens})


def test_absent_cache_counters_are_unknown_and_zero_is_not_proof():
    result = summarize([observed({'input_tokens': 100}),
                        observed({'cached_input_tokens': 0}, seq=2)])
    absent, zero = [item['cache'] for item in result['observations']]
    assert absent['read_tokens_reported'] is None
    assert absent['write_tokens_reported'] is None
    assert absent['reason'] == 'not_reported'
    assert zero['read_tokens_reported'] == 0
    assert zero['reason'] == 'native_report_only'
    assert zero['zero_is_conclusive'] is False
    assert zero['upstream_verified'] is False and zero['cost'] is None


def test_cache_counts_keep_scope_and_do_not_create_double_counted_totals():
    result = summarize([
        observed({'cached_input_tokens': 200, 'cache_write_input_tokens': 40},
                 scope='session', aggregation='cumulative'),
        observed({'cached_input_tokens': 80}, scope='observation', aggregation='latest', seq=2),
    ])
    assert 'cache_read_tokens' not in result and 'total_tokens' not in result
    total, latest = [item['cache'] for item in result['observations']]
    assert (total['scope'], total['aggregation'], total['read_tokens_reported']) == (
        'session', 'cumulative', 200)
    assert latest['read_tokens_reported'] == 80 and latest['scope'] == 'observation'
    assert total['write_tokens_reported'] == 40
    assert latest['write_tokens_reported'] is None


def test_invalid_counters_remain_unknown_and_empty_evidence_is_not_success():
    result = summarize([observed({'cached_input_tokens': True, 'cache_write_input_tokens': -1})])
    assert result['observations'][0]['cache']['reason'] == 'not_reported'
    assert summarize([]) == {'supported': False, 'observations': [], 'reason': 'not_reported'}
