"""Show observed provider quota windows without inventing available capacity."""

from datetime import datetime, timedelta, timezone

from test_playground_browser import _launch_browser, local_playground, playwright_api
from test_playground_browser_layout import _assert_layout_fits, _observed_page


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def test_claude_usage_shows_distinct_short_and_weekly_windows(local_playground):
    entry = local_playground['claude']['/v0/management/auth-files'][1]['files'][0]
    now = datetime.now(timezone.utc)
    entry['quota'] = {
        'observed_at': _stamp(now),
        'signals': {
            'Anthropic-Ratelimit-Unified-5h-Utilization': '0.24',
            'Anthropic-Ratelimit-Unified-5h-Reset': str(int((now + timedelta(hours=2)).timestamp())),
            'Anthropic-Ratelimit-Unified-7d-Utilization': '0.63',
            'Anthropic-Ratelimit-Unified-7d-Reset': str(int((now + timedelta(days=3)).timestamp())),
        },
    }
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-accounts').click()
            row = page.get_by_test_id('account-row').filter(has_text='Claude Research')
            row.get_by_test_id('account-usage-open').click()
            dialog = page.get_by_test_id('account-usage-dialog')
            dialog.wait_for(state='visible')
            windows = dialog.get_by_test_id('account-usage-window')
            assert windows.count() == 2
            five_hour = windows.filter(has_text='24%')
            weekly = windows.filter(has_text='63%')
            five_hour.get_by_text('5h', exact=False).wait_for()
            weekly.get_by_text('7d', exact=False).wait_for()
            assert 'reset' in five_hour.inner_text().lower()
            assert 'reset' in weekly.inner_text().lower()
            dialog.get_by_text('The latest direct quota check did not complete', exact=False).wait_for()
            assert row.get_by_text('Unknown usage').count() == 0
            for width, height in ((1440, 1000), (390, 844), (320, 700)):
                page.set_viewport_size({'width': width, 'height': height})
                _assert_layout_fits(page)
            assert errors == []
        finally:
            browser.close()


def test_old_codex_percentage_is_hidden_after_refresh(local_playground):
    entry = local_playground['openai']['/v0/management/auth-files'][1]['files'][0]
    entry['quota']['observed_at'] = _stamp(datetime.now(timezone.utc) - timedelta(hours=2))
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-accounts').click()
            row = page.get_by_test_id('account-row').filter(has_text='OpenAI Personal')
            row.get_by_text('Current usage unavailable').wait_for()
            assert row.get_by_text('58% used').count() == 0
            row.get_by_test_id('account-usage-open').click()
            dialog = page.get_by_test_id('account-usage-dialog')
            dialog.get_by_text('Current usage unavailable').wait_for()
            assert dialog.get_by_text('58% used').count() == 0
            assert dialog.get_by_text('Outdated readings', exact=False).count() == 0
            dialog.locator('#account-usage-close').click()
            page.get_by_test_id('nav-overview').click()
            overview = page.get_by_test_id('overview-capacity-row').filter(has_text='OpenAI Personal')
            overview.get_by_text('Current usage unavailable').wait_for()
            assert overview.get_by_text('58% used').count() == 0
            page.locator('#metric-fresh').get_by_text('0').wait_for()
            assert errors == []
        finally:
            browser.close()


def test_reported_overage_keeps_its_value_without_overflow(local_playground):
    entry = local_playground['claude']['/v0/management/auth-files'][1]['files'][0]
    entry['quota'] = {'observed_at': _stamp(datetime.now(timezone.utc)),
                      'signals': {'Anthropic-Ratelimit-Unified-5h-Utilization': '1.02'}}
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-accounts').click()
            row = page.get_by_test_id('account-row').filter(has_text='Claude Research')
            row.get_by_text('102% used').wait_for()
            row.get_by_test_id('account-usage-open').click()
            dialog = page.get_by_test_id('account-usage-dialog')
            window = dialog.get_by_test_id('account-usage-window')
            window.get_by_text('102% used').wait_for()
            assert window.locator('.usage-fill').evaluate('(fill) => fill.style.width') == '100%'
            _assert_layout_fits(page)
            assert errors == []
        finally:
            browser.close()


def test_fresh_account_window_excludes_old_model_detail(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            order = page.evaluate('''async () => {
              const { usageWindows } = await import('/static/usage-view.js');
              return usageWindows({ stale: false, quota_windows: [
                { id: 'old-model', label: 'Model quota', scope: 'model', model_id: 'example',
                  used_percent: 91, stale: true },
                { id: 'current-account', label: 'Account quota', scope: 'account',
                  used_percent: 37, stale: false,
                  stale_at: new Date(Date.now() + 60000).toISOString() },
              ] }).map((window) => window.id);
            }''')
            assert order == ['current-account']
            assert errors == []
        finally:
            browser.close()


def _timed_quota(used_percent, *, seconds_until_expiry, stale=False):
    now = datetime.now(timezone.utc)
    return {
        'account_id': 'openai-personal', 'source': 'fixture', 'scope': 'account',
        'supported': True, 'stale': stale,
        'reason': 'upstream_quota_stale' if stale else None,
        'quota_windows': [{
            'id': 'timed', 'label': 'Timed quota', 'scope': 'account',
            'used_percent': used_percent, 'remaining_percent': 100 - used_percent,
            'window_seconds': 3600, 'observed_at': _stamp(now),
            'stale_at': _stamp(now + timedelta(seconds=seconds_until_expiry)),
            'stale': stale,
        }],
    }


def test_expired_quota_refreshes_only_affected_account(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            calls = {'openai': 0, 'claude': 0}

            def timed_usage(route):
                if 'OpenAI%20Personal' not in route.request.url:
                    calls['claude'] += 1
                    route.continue_()
                    return
                calls['openai'] += 1
                current = calls['openai'] > 1
                snapshot = _timed_quota(37 if current else 11,
                                        seconds_until_expiry=60 if current else 4)
                route.fulfill(status=200, json={'result': snapshot})

            page.route('**/api/accounts/*/usage*', timed_usage)
            page.get_by_test_id('nav-accounts').click()
            page.locator('#refresh-button').click()
            row = page.get_by_test_id('account-row').filter(has_text='OpenAI Personal')
            row.get_by_text('11% used').wait_for()
            other_before_expiry = calls['claude']
            page.evaluate('''() => {
              const originalFetch = window.fetch.bind(window);
              window.fetch = async (...args) => {
                const response = await originalFetch(...args);
                const path = String(args[0]);
                if (path.includes('OpenAI%20Personal/usage?refresh=1')) {
                  await new Promise((release) => { window.releaseQuotaResponse = release; });
                }
                return response;
              };
            }''')
            row.get_by_text('Current usage unavailable').wait_for(timeout=10000)
            assert page.evaluate('typeof window.releaseQuotaResponse') == 'function'
            assert row.get_by_text('11% used').count() == 0
            assert row.get_by_text('37% used').count() == 0
            page.evaluate('window.releaseQuotaResponse()')
            row.get_by_text('37% used').wait_for(timeout=10000)
            assert row.get_by_text('11% used').count() == 0
            assert calls['openai'] == 2
            assert calls['claude'] == other_before_expiry
            assert errors == []
        finally:
            browser.close()


def test_failed_expiry_refresh_hides_old_percentage(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            calls = []

            def failed_usage(route):
                if 'OpenAI%20Personal' not in route.request.url:
                    route.continue_()
                    return
                calls.append(route.request.url)
                stale = len(calls) > 1
                snapshot = _timed_quota(13, seconds_until_expiry=-1 if stale else 2,
                                        stale=stale)
                if stale:
                    snapshot['refresh_reason'] = 'upstream_quota_unavailable'
                route.fulfill(status=200, json={'result': snapshot})

            page.route('**/api/accounts/*/usage*', failed_usage)
            page.get_by_test_id('nav-accounts').click()
            page.locator('#refresh-button').click()
            row = page.get_by_test_id('account-row').filter(has_text='OpenAI Personal')
            row.get_by_text('13% used').wait_for()
            row.get_by_test_id('account-usage-open').click()
            dialog = page.get_by_test_id('account-usage-dialog')
            dialog.get_by_text('13% used').wait_for()
            with page.expect_response(lambda response:
                    'OpenAI%20Personal/usage?refresh=1' in response.url and len(calls) > 1,
                    timeout=10000):
                row.get_by_text('Current usage unavailable').wait_for(timeout=10000)
            dialog.get_by_text('Current usage unavailable').wait_for()
            assert dialog.get_by_text('13% used').count() == 0
            assert row.get_by_text('13% used').count() == 0
            assert dialog.get_by_text('Outdated readings', exact=False).count() == 0
            dialog.locator('#account-usage-close').click()
            page.get_by_test_id('nav-overview').click()
            overview = page.get_by_test_id('overview-capacity-row').filter(has_text='OpenAI Personal')
            overview.get_by_text('Current usage unavailable').wait_for()
            assert overview.get_by_text('13% used').count() == 0
            assert len(calls) == 2
            assert errors == []
        finally:
            browser.close()
