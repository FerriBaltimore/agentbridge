"""Spend caps must survive normalizing accounts without rolling windows."""
import pytest

from agentbridge.quota_spend import codex_spend


def test_windowless_pool_keeps_exhausted_spend_cap():
    raw = {'rateLimits': {'limitId': 'codex', 'primary': None, 'secondary': None,
           'spendControlReached': True, 'rateLimitReachedType': 'workspace_member_usage_limit_reached',
           'individualLimit': {'limit': '100.00', 'used': '100.00', 'remainingPercent': 0, 'resetsAt': 2000}}}
    pool = codex_spend(raw, now=1000)[0]
    assert pool['pool_id'] == 'codex' and pool['spend_control_reached'] is True
    assert pool['rate_limit_reached_type'] == 'workspace_member_usage_limit_reached'
    limit = pool['individual_limit']
    assert limit['remaining_percent'] == 0 and limit['used_percent'] == 100
    assert limit['reset_after_seconds'] == 1000 and limit['reset_due'] is False
    assert limit['limit_reached'] is True and limit['unit'] is None
    assert limit['limit'] == limit['used'] == '100.00'


def test_unknown_values_never_become_false_or_zero():
    result = codex_spend({'rateLimits': {'credits': {}, 'individualLimit': {}}}, now=1000)[0]
    assert result['spend_control_reached'] is None
    assert result['credits'] == {'has_credits': None, 'unlimited': None, 'balance': None, 'unit': None}
    assert result['individual_limit']['remaining_percent'] is None
    assert result['individual_limit']['limit_reached'] is None
    assert result['individual_limit']['resets_at'] is None
    assert result['individual_limit']['reset_after_seconds'] is None


def test_multi_bucket_mirrors_are_not_duplicated_or_summed():
    one = {'limitId': 'codex', 'planType': 'pro', 'credits': {'hasCredits': True, 'unlimited': False, 'balance': '25.17'}}
    two = {'limitId': 'other-pool', 'credits': {'hasCredits': True, 'unlimited': True, 'balance': None}}
    result = codex_spend({'rateLimits': one, 'rateLimitsByLimitId': {'codex': one, 'other-pool': two}}, now=0)
    assert [pool['pool_id'] for pool in result] == ['codex', 'other-pool']
    assert result[0]['plan'] == 'pro' and result[0]['credits']['balance'] == '25.17'
    assert result[1]['credits']['unlimited'] is True and result[1]['credits']['balance'] is None


def test_passed_reset_does_not_invent_renewed_spend_capacity():
    raw = {'rateLimits': {'spendControlReached': True,
                         'individualLimit': {'remainingPercent': 0, 'resetsAt': 100}}}
    result = codex_spend(raw, now=200)[0]
    assert result['spend_control_reached'] is True
    assert result['individual_limit']['reset_due'] is True
    assert result['individual_limit']['reset_after_seconds'] == 0
    assert result['individual_limit']['limit_reached'] is True


@pytest.mark.parametrize('bad', [True, False, -1, float('nan'), float('inf'), 101, '50'])
def test_invalid_remaining_percent_is_unknown(bad):
    result = codex_spend({'rateLimits': {'individualLimit': {'remainingPercent': bad}}})[0]
    assert result['individual_limit']['remaining_percent'] is None


@pytest.mark.parametrize('bad', ['NaN', 'Infinity', '-10', 'secret-value', 100, True, None])
def test_amounts_preserve_only_nonnegative_finite_decimal_strings(bad):
    result = codex_spend({'rateLimits': {'credits': {'balance': bad}, 'individualLimit': {'limit': bad, 'used': bad}}})[0]
    assert result['credits']['balance'] is None
    assert result['individual_limit']['limit'] is None
    assert result['individual_limit']['used'] is None


def test_reset_credits_are_not_mistaken_for_spend_balance():
    result = codex_spend({'rateLimitResetCredits': {'availableCount': 3}, 'rateLimits': {}}, now=0)[0]
    assert result['credits'] is None
    assert 'available_count' not in result
