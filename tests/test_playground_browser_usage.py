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
            dialog.get_by_text('Current quota refresh unavailable', exact=False).wait_for()
            assert row.get_by_text('Unknown usage').count() == 0
            for width, height in ((1440, 1000), (390, 844), (320, 700)):
                page.set_viewport_size({'width': width, 'height': height})
                _assert_layout_fits(page)
            assert errors == []
        finally:
            browser.close()


def test_old_codex_percentage_remains_visible_but_is_marked_stale(local_playground):
    entry = local_playground['openai']['/v0/management/auth-files'][1]['files'][0]
    entry['quota']['observed_at'] = _stamp(datetime.now(timezone.utc) - timedelta(hours=2))
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-accounts').click()
            row = page.get_by_test_id('account-row').filter(has_text='OpenAI Personal')
            row.get_by_text('58% used').wait_for()
            row.get_by_text('stale', exact=False).wait_for()
            page.get_by_test_id('nav-overview').click()
            overview = page.get_by_test_id('overview-capacity-row').filter(has_text='OpenAI Personal')
            overview.get_by_text('58% used').wait_for()
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


def test_fresh_account_window_precedes_old_model_detail(local_playground):
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
                  used_percent: 37, stale: false },
              ] }).map((window) => window.id);
            }''')
            assert order == ['current-account', 'old-model']
            assert errors == []
        finally:
            browser.close()
