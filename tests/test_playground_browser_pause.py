"""Browser coverage for page memory, quota dialog, and account pause."""

from datetime import datetime, timedelta, timezone

from test_playground_browser import _launch_browser, local_playground, playwright_api
from test_playground_browser_layout import _assert_layout_fits, _observed_page


def test_browser_keeps_each_page_after_reload(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            for view in ('accounts', 'chat', 'activity', 'overview'):
                page.get_by_test_id(f'nav-{view}').click()
                assert page.url.endswith(f'#{view}')
                page.reload(wait_until='networkidle')
                assert page.locator(f'#view-{view}').is_visible()
                assert page.get_by_test_id(f'nav-{view}').get_attribute('aria-current') == 'page'
            assert errors == []
        finally:
            browser.close()


def test_usage_dialog_closes_and_pause_updates_routing(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-accounts').click()
            row = page.get_by_test_id('account-row').filter(has_text='OpenAI Personal')
            row.get_by_test_id('account-usage-open').click()
            dialog = page.get_by_test_id('account-usage-dialog')
            dialog.get_by_text('Primary and secondary', exact=False).wait_for()
            dialog.locator('#account-usage-close').click()
            dialog.wait_for(state='hidden')
            row.get_by_test_id('account-usage-open').click()
            page.keyboard.press('Escape')
            dialog.wait_for(state='hidden')
            row.get_by_test_id('account-pause-toggle').click()
            row.get_by_text('Paused', exact=True).wait_for()
            assert local_playground['bridge'].account_status('OpenAI Personal')['routing']['paused']
            assert not any(item.account_id == 'openai-personal' for item in
                           local_playground['bridge'].routes.candidates('fixture/openai-model'))
            page.reload(wait_until='networkidle')
            row = page.get_by_test_id('account-row').filter(has_text='OpenAI Personal')
            row.get_by_text('Paused', exact=True).wait_for()
            row.get_by_test_id('account-pause-toggle').click()
            row.get_by_text('active', exact=True).wait_for()
            assert not local_playground['bridge'].account_status('OpenAI Personal')['routing']['paused']
            for width, height in ((1440, 1000), (800, 800), (390, 844), (320, 700)):
                page.set_viewport_size({'width': width, 'height': height})
                _assert_layout_fits(page)
                if width == 800:
                    assert page.locator('#accounts-list').evaluate(
                        '(list) => list.scrollWidth - list.clientWidth') <= 1
            assert errors == []
        finally:
            browser.close()


def test_seven_usage_windows_fit_and_close_on_mobile(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

            def seven_windows(route):
                if 'OpenAI%20Personal' not in route.request.url:
                    route.continue_()
                    return
                response = route.fetch()
                payload = response.json()
                original = payload['result']['quota_windows'][0]
                payload['result']['quota_windows'] = [
                    {**original, 'id': f'fixture-{index}',
                     'label': f'fixture-pool-{index} · primary',
                     'used_percent': 10 * index,
                     'stale': index >= 4,
                     'stale_at': future}
                    for index in range(1, 8)
                ]
                route.fulfill(response=response, json=payload)

            page.route('**/api/accounts/*/usage*', seven_windows)
            page.get_by_test_id('nav-accounts').click()
            page.locator('#refresh-button').click()
            row = page.get_by_test_id('account-row').filter(has_text='OpenAI Personal')
            row.get_by_text('View 7 windows').wait_for()
            row.get_by_test_id('account-usage-open').click()
            dialog = page.get_by_test_id('account-usage-dialog')
            assert dialog.get_by_test_id('account-usage-window').count() == 7
            dialog.get_by_text('fixture-pool-1 is a provider-reported quota group', exact=False).wait_for()
            dialog.get_by_text('Older observations · 4').wait_for()
            dialog.locator('.usage-older-section summary').click()
            for width, height in ((1440, 1000), (390, 844), (320, 700)):
                page.set_viewport_size({'width': width, 'height': height})
                _assert_layout_fits(page)
                assert dialog.locator('#account-usage-done').is_visible()
            dialog.locator('.modal-body').evaluate('(body) => body.scrollTop = body.scrollHeight')
            dialog.locator('#account-usage-done').click()
            dialog.wait_for(state='hidden')
            assert errors == []
        finally:
            browser.close()
