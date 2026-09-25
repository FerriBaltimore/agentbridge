"""Earned reset controls use SDK-backed fixture responses, never real credits."""

from agentbridge import BridgeError
from test_playground_browser import _launch_browser, _now, _page, local_playground, playwright_api
from test_playground_browser_layout import _assert_layout_fits, _observed_page


def _snapshot(*, count=2, status='available'):
    return {
        'account_id': 'openai-personal', 'account_ref': 'OpenAI Personal',
        'provider': 'codex', 'status': status, 'available_count': count,
        'credits': ([{'id': 'fixture-credit', 'status': 'available',
                     'expires_at': '2026-12-31T00:00:00Z'}] if count else []),
        'observed_at': _now(), 'stale': False,
        'observation_ref': 'fixture-observation',
    }


def _open_codex_usage(page):
    page.get_by_test_id('nav-accounts').click()
    row = page.get_by_test_id('account-row').filter(has_text='OpenAI Personal')
    row.get_by_test_id('account-usage-open').click()
    return page.get_by_test_id('account-usage-dialog')


def test_reset_requires_confirmation_and_updates_count(local_playground, monkeypatch):
    bridge = local_playground['bridge']
    reads, writes = [], []

    def credits(account_ref, *, refresh):
        reads.append((account_ref, refresh))
        return _snapshot()

    def redeem(account_ref, *, idempotency_key, observation_ref, credit_id=None):
        writes.append((account_ref, idempotency_key, observation_ref, credit_id))
        return {'outcome': 'reset', 'reset_credits': _snapshot(count=1)}

    monkeypatch.setattr(bridge, 'account_reset_credits', credits, raising=False)
    monkeypatch.setattr(bridge, 'account_quota_reset', redeem, raising=False)
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            dialog = _open_codex_usage(page)
            reset = dialog.get_by_test_id('account-reset-section')
            reset.get_by_text('2 earned resets available').wait_for()
            reset.get_by_text('Credit details · 1').click()
            reset.get_by_text('Expires', exact=False).wait_for()
            assert reads == [('OpenAI Personal', True)]
            reset.get_by_test_id('account-reset-use').click()
            reset.get_by_test_id('account-reset-confirmation').wait_for()
            assert writes == []
            reset.get_by_text('Cancel', exact=True).click()
            assert writes == []
            reset.get_by_test_id('account-reset-use').click()
            reset.get_by_test_id('account-reset-confirm').click()
            reset.get_by_text('1 earned reset available').wait_for()
            assert len(writes) == 1
            assert writes[0][0] == 'OpenAI Personal'
            assert writes[0][2:] == ('fixture-observation', 'fixture-credit')
            assert page.evaluate("localStorage.getItem('agentbridge.quota-reset:openai-personal')") is None
            for width, height in ((1440, 1000), (390, 844), (320, 700)):
                page.set_viewport_size({'width': width, 'height': height})
                _assert_layout_fits(page)
            assert errors == []
        finally:
            browser.close()


def test_unknown_outcome_reuses_saved_key_after_browser_reload(local_playground, monkeypatch):
    bridge = local_playground['bridge']
    writes = []
    state = {'count': 2}

    def credits(account_ref, *, refresh):
        assert account_ref == 'OpenAI Personal' and refresh
        return _snapshot(count=state['count'], status='available' if state['count'] else 'none')

    def redeem(account_ref, *, idempotency_key, observation_ref, credit_id=None):
        writes.append((idempotency_key, observation_ref, credit_id))
        if len(writes) == 1:
            state['count'] = 0
            raise BridgeError('reset_outcome_unknown', 'The reset outcome is unknown.')
        return {'outcome': 'already_redeemed', 'reset_credits': _snapshot(count=0, status='none')}

    monkeypatch.setattr(bridge, 'account_reset_credits', credits, raising=False)
    monkeypatch.setattr(bridge, 'account_quota_reset', redeem, raising=False)
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            dialog = _open_codex_usage(page)
            reset = dialog.get_by_test_id('account-reset-section')
            reset.get_by_text('2 earned resets available').wait_for()
            reset.get_by_test_id('account-reset-use').click()
            reset.get_by_test_id('account-reset-confirm').click()
            reset.get_by_text('A reset attempt has an unknown outcome', exact=False).wait_for()
            saved = page.evaluate("localStorage.getItem('agentbridge.quota-reset:openai-personal')")
            assert saved and len(writes) == 1
            page.reload(wait_until='networkidle')
            dialog = _open_codex_usage(page)
            reset = dialog.get_by_test_id('account-reset-section')
            reset.get_by_text('No earned resets available').wait_for()
            reset.get_by_text('A reset attempt has an unknown outcome', exact=False).wait_for()
            assert page.evaluate("localStorage.getItem('agentbridge.quota-reset:openai-personal')") == saved
            reset.get_by_test_id('account-reset-use').click()
            reset.get_by_test_id('account-reset-confirm').click()
            reset.get_by_text('A reset attempt has an unknown outcome', exact=False).wait_for(state='hidden')
            assert len(writes) == 2 and writes[0] == writes[1]
            assert page.evaluate("localStorage.getItem('agentbridge.quota-reset:openai-personal')") is None
            assert errors == []
        finally:
            browser.close()


def test_zero_credits_has_no_redeem_action(local_playground, monkeypatch):
    bridge = local_playground['bridge']
    monkeypatch.setattr(bridge, 'account_reset_credits',
                        lambda account_ref, *, refresh: _snapshot(count=0, status='none'),
                        raising=False)
    monkeypatch.setattr(bridge, 'account_quota_reset',
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('must not redeem')),
                        raising=False)
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            reset = _open_codex_usage(page).get_by_test_id('account-reset-section')
            reset.get_by_text('No earned resets available').wait_for()
            assert reset.get_by_test_id('account-reset-use').count() == 0
            assert errors == []
        finally:
            browser.close()


def test_unavailable_browser_storage_stops_before_provider_mutation(local_playground, monkeypatch):
    bridge = local_playground['bridge']
    calls = []
    monkeypatch.setattr(bridge, 'account_reset_credits',
                        lambda account_ref, *, refresh: _snapshot(), raising=False)
    monkeypatch.setattr(bridge, 'account_quota_reset',
                        lambda *args, **kwargs: calls.append((args, kwargs)), raising=False)
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            reset = _open_codex_usage(page).get_by_test_id('account-reset-section')
            reset.get_by_text('2 earned resets available').wait_for()
            page.evaluate('''() => {
              const original = Storage.prototype.setItem;
              Storage.prototype.setItem = function (key, value) {
                if (key.startsWith('agentbridge.quota-reset:')) throw new Error('storage blocked');
                return original.call(this, key, value);
              };
            }''')
            reset.get_by_test_id('account-reset-use').click()
            reset.get_by_test_id('account-reset-confirm').click()
            reset.get_by_text('No reset request was sent', exact=False).wait_for()
            assert calls == [] and errors == []
        finally:
            browser.close()


def test_sdk_pending_receipt_can_be_resolved_without_local_storage(local_playground, monkeypatch):
    bridge = local_playground['bridge']
    writes = []

    def credits(account_ref, *, refresh):
        return {**_snapshot(count=0, status='none'), 'pending_reset': {
            'idempotency_key': 'fixture-saved-request',
            'observation_ref': 'fixture-old-observation',
            'credit_id': 'fixture-credit'}}

    def redeem(account_ref, *, idempotency_key, observation_ref, credit_id=None):
        writes.append((idempotency_key, observation_ref, credit_id))
        return {'outcome': 'already_redeemed', 'reset_credits': _snapshot(count=0, status='none')}

    monkeypatch.setattr(bridge, 'account_reset_credits', credits, raising=False)
    monkeypatch.setattr(bridge, 'account_quota_reset', redeem, raising=False)
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            reset = _open_codex_usage(page).get_by_test_id('account-reset-section')
            reset.get_by_text('A reset attempt has an unknown outcome', exact=False).wait_for()
            reset.get_by_test_id('account-reset-use').click()
            reset.get_by_test_id('account-reset-confirm').click()
            reset.get_by_text('A reset attempt has an unknown outcome', exact=False).wait_for(state='hidden')
            assert writes == [('fixture-saved-request', 'fixture-old-observation', 'fixture-credit')]
            assert errors == []
        finally:
            browser.close()


def test_definitive_preflight_refusal_clears_new_local_attempt(local_playground, monkeypatch):
    bridge = local_playground['bridge']
    writes = []
    monkeypatch.setattr(bridge, 'account_reset_credits',
                        lambda account_ref, *, refresh: _snapshot(), raising=False)

    def refuse(account_ref, *, idempotency_key, observation_ref, credit_id=None):
        writes.append(idempotency_key)
        raise BridgeError('reset_observation_stale', 'The reset observation is stale.')

    monkeypatch.setattr(bridge, 'account_quota_reset', refuse, raising=False)
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            reset = _open_codex_usage(page).get_by_test_id('account-reset-section')
            reset.get_by_text('2 earned resets available').wait_for()
            reset.get_by_test_id('account-reset-use').click()
            reset.get_by_test_id('account-reset-confirm').click()
            reset.get_by_text('The reset observation is stale', exact=False).wait_for()
            assert len(writes) == 1
            assert page.evaluate("localStorage.getItem('agentbridge.quota-reset:openai-personal')") is None
            assert reset.get_by_text('A reset attempt has an unknown outcome', exact=False).count() == 0
            assert errors == []
        finally:
            browser.close()
