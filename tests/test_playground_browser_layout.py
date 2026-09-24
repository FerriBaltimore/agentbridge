"""Check playground layout and uncertain data states in a real browser."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Thread

import pytest

from agentbridge import Account, Bridge
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from playground.server import create_server
from test_playground_browser import (
    _launch_browser,
    _proxy_responses,
    local_playground,
    playwright_api,
)
from test_proxy_management import local_management


@contextmanager
def _running_playground(tmp_path, bridge):
    server = create_server(tmp_path / 'state', port=0, bridge=bridge,
                           workspace_path=tmp_path)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.fixture
def empty_playground(tmp_path):
    with Bridge(tmp_path / 'state') as bridge:
        with _running_playground(tmp_path, bridge) as url:
            yield url


@pytest.fixture
def unknown_playground(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_UNKNOWN_CLIENT_KEY', 'fixture-client-key')
    monkeypatch.setenv('LAB_UNKNOWN_MANAGEMENT_KEY', 'fixture-management-key')
    responses = _proxy_responses('claude', 'fixture/unknown-metadata')
    responses['/v1/models?client_version=pi'] = (200, {'data': []}, {})
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            account = Account('unknown-observation', 'codex', name='Unknown Signals',
                              provider='claude', supported_models=('fixture/unknown-metadata',),
                              proxy_base_url=f'http://127.0.0.1:{port}/v1',
                              key_env='LAB_UNKNOWN_CLIENT_KEY',
                              management_key_env='LAB_UNKNOWN_MANAGEMENT_KEY')
            seed_authenticated_proxy_account(bridge.store, account, observe_local=True)
            with _running_playground(tmp_path, bridge) as url:
                yield url


def _browser_errors(page):
    errors = []
    page.on('console', lambda message: errors.append(
        f'{message.text} @ {message.location}')
            if message.type == 'error' else None)
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('response', lambda response: errors.append(
        f'HTTP {response.status}: {response.url}') if response.status >= 400 else None)
    return errors


def _observed_page(browser, url):
    page = browser.new_page(viewport={'width': 1440, 'height': 1000},
                            device_scale_factor=1)
    errors = _browser_errors(page)
    page.goto(url, wait_until='networkidle')
    return page, errors


def _assert_layout_fits(page):
    metrics = page.evaluate('''() => ({
      documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      dialogs: [...document.querySelectorAll('dialog[open]')].map((dialog) => {
        const box = dialog.getBoundingClientRect();
        return {
          overflow: dialog.scrollWidth - dialog.clientWidth,
          left: box.left,
          right: box.right,
        };
      }),
    })''')
    assert metrics['documentOverflow'] <= 1, metrics
    for dialog in metrics['dialogs']:
        assert dialog['overflow'] <= 1, metrics
        assert dialog['left'] >= -1 and dialog['right'] <= page.viewport_size['width'] + 1, metrics


def _assert_compact_view(page, view, first_content):
    section = page.locator(f'#view-{view}')
    heading = section.locator('h1')
    assert heading.count() == 1
    assert heading.get_attribute('class') == 'visually-hidden'
    assert section.locator('.hero, .page-intro').count() == 0
    offset = page.evaluate('''([view, selector]) => {
      const section = document.querySelector(`#view-${view}`);
      return section.querySelector(selector).getBoundingClientRect().top
        - section.getBoundingClientRect().top;
    }''', [view, first_content])
    assert offset <= 20, (view, offset)


def test_responsive_views_dialogs_and_keyboard_navigation(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            for width, height in [(1440, 1000), (390, 844), (320, 700)]:
                page.set_viewport_size({'width': width, 'height': height})
                page.get_by_test_id('nav-overview').click()
                page.get_by_role('heading', name='Available routes').wait_for()
                _assert_compact_view(page, 'overview', '.metric-grid')
                assert page.get_by_test_id('add-account').is_hidden()
                _assert_layout_fits(page)

                page.get_by_test_id('nav-activity').click()
                page.locator('#activity-title').get_by_text('No conversation selected').wait_for()
                page.get_by_test_id('activity-list').get_by_text('No events recorded').wait_for()
                _assert_compact_view(page, 'activity', '.activity-surface')
                assert page.get_by_role('button', name='Refresh events').is_visible()
                assert page.get_by_test_id('add-account').is_hidden()
                _assert_layout_fits(page)

                page.get_by_test_id('nav-accounts').click()
                page.get_by_test_id('accounts-list').get_by_text('OpenAI Personal').wait_for()
                _assert_compact_view(page, 'accounts', '.view-actions')
                assert page.get_by_role('button', name='Add account').count() == 1
                _assert_layout_fits(page)

                page.get_by_test_id('nav-chat').click()
                _assert_compact_view(page, 'chat', '.view-actions')
                assert page.get_by_role('button', name='New conversation').is_visible()
                _assert_layout_fits(page)
                page.get_by_test_id('nav-accounts').click()

                page.get_by_test_id('add-account').click()
                dialog = page.get_by_test_id('login-dialog')
                dialog.wait_for(state='visible')
                assert page.evaluate('document.activeElement.id') == 'login-name'
                page.keyboard.press('Shift+Tab')
                assert page.evaluate('document.activeElement.id') == 'login-provider'
                page.keyboard.press('Tab')
                assert page.evaluate('document.activeElement.id') == 'login-name'
                _assert_layout_fits(page)
                if width == 390:
                    page.screenshot(path=str(local_playground['screenshot_dir'] /
                                             'playground-login-mobile.png'))
                page.keyboard.press('Escape')
                dialog.wait_for(state='hidden')

                row = page.get_by_test_id('accounts-list').get_by_test_id('account-row').filter(
                    has_text='OpenAI Personal')
                row.get_by_test_id('remove-account').click()
                removal = page.get_by_test_id('remove-dialog')
                removal.wait_for(state='visible')
                assert page.evaluate('document.activeElement.id') == 'remove-confirm'
                removal.get_by_text('Its provider authorization is not revoked.').wait_for()
                _assert_layout_fits(page)
                if width == 390:
                    page.screenshot(path=str(local_playground['screenshot_dir'] /
                                             'playground-remove-mobile.png'))
                page.keyboard.press('Escape')
                removal.wait_for(state='hidden')
                row.get_by_text('OpenAI Personal').wait_for()
            assert errors == []
        finally:
            browser.close()


def test_empty_workspace_has_honest_states_and_no_overflow(empty_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, empty_playground)
            page.locator('#metric-accounts').get_by_text('0').wait_for()
            page.locator('#overview-capacity-list').get_by_text('No account capacity yet').wait_for()
            page.locator('#metric-ready').get_by_text('0').wait_for()
            page.locator('#metric-fresh').get_by_text('0').wait_for()
            assert page.locator('#overview-open-chat').is_hidden()
            assert page.locator('#overview-manage-accounts').inner_text() == 'Connect an account'
            page.locator('#overview-manage-accounts').click()
            assert page.get_by_test_id('add-account').is_visible()
            for width, height in [(1440, 1000), (390, 844), (320, 700)]:
                page.set_viewport_size({'width': width, 'height': height})
                for view in ['overview', 'accounts', 'chat', 'activity']:
                    page.get_by_test_id(f'nav-{view}').click()
                    first_content = {
                        'overview': '.metric-grid', 'accounts': '.view-actions',
                        'chat': '.view-actions', 'activity': '.activity-surface',
                    }[view]
                    _assert_compact_view(page, view, first_content)
                    _assert_layout_fits(page)
                page.get_by_test_id('nav-accounts').click()
                page.get_by_test_id('accounts-list').get_by_text('No accounts connected').wait_for()
                assert page.get_by_role('button', name='Add account').count() == 1
                assert page.locator('#accounts-summary').is_hidden()
                assert page.locator('#accounts-removal-note').is_hidden()
                page.get_by_test_id('nav-chat').click()
                assert page.get_by_test_id('chat-send').is_disabled()
                assert page.get_by_test_id('chat-model').locator('option').all_text_contents() == [
                    'No observed models']
                page.get_by_test_id('nav-activity').click()
                page.get_by_test_id('activity-list').get_by_text('No events recorded').wait_for()
            assert errors == []
        finally:
            browser.close()


def test_unknown_usage_and_missing_model_metadata_stay_unknown(unknown_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, unknown_playground)
            page.get_by_test_id('nav-overview').click()
            page.locator('#overview-capacity-list').get_by_text('Unknown usage').wait_for()
            page.get_by_test_id('nav-accounts').click()
            row = page.get_by_test_id('accounts-list').get_by_test_id('account-row')
            row.get_by_text('Unknown Signals').wait_for()
            row.get_by_text('Unknown usage').wait_for()
            row.get_by_test_id('account-models-open').click()
            dialog = page.get_by_test_id('account-models-dialog')
            dialog.get_by_text('Usage unknown').wait_for()
            assert dialog.get_by_text('Effort:', exact=False).count() == 0
            assert dialog.get_by_text('Context:', exact=False).count() == 0
            page.keyboard.press('Escape')
            dialog.wait_for(state='hidden')

            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-model').select_option('fixture/unknown-metadata')
            assert page.locator('#effort-field').is_hidden()
            assert page.locator('#context-field').is_hidden()
            assert page.locator('#chat-effort').locator('option').all_text_contents() == ['Default']
            assert page.locator('#chat-context').input_value() == ''
            page.set_viewport_size({'width': 320, 'height': 700})
            _assert_layout_fits(page)
            assert errors == []
        finally:
            browser.close()


def test_overview_capacity_paginates_to_available_height(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-overview').click()
            page.set_viewport_size({'width': 1440, 'height': 900})
            page.evaluate('''async () => {
              const { renderOverview } = await import('/static/overview.js');
              const accounts = Array.from({ length: 16 }, (_, index) => ({
                account_ref: `fixture-capacity-${index + 1}`,
                name: `Capacity ${String(index + 1).padStart(2, '0')}`,
                provider: 'codex',
              }));
              renderOverview({ accounts, models: { items: [] }, instances: [],
                statuses: new Map(), usage: new Map() });
            }''')
            rows = page.get_by_test_id('overview-capacity-row')
            playwright_api.expect(rows.first).to_be_visible()
            desktop_count = rows.count()
            assert 1 < desktop_count < 16
            pager = page.locator('#overview-page-controls')
            assert pager.is_visible()
            assert page.locator('#overview-page-status').inner_text().endswith('of 16 accounts')
            page.locator('#overview-next-page').click()
            assert rows.first.get_by_text('Capacity 01').count() == 0

            page.set_viewport_size({'width': 320, 'height': 700})
            page.wait_for_function('''desktopCount =>
              document.querySelectorAll('[data-testid="overview-capacity-row"]').length < desktopCount''',
                                   arg=desktop_count)
            assert rows.count() < desktop_count
            assert page.evaluate('document.documentElement.scrollHeight - innerHeight') <= 1
            _assert_layout_fits(page)
            assert errors == []
        finally:
            browser.close()


def test_overview_does_not_count_stale_quota_as_fresh(local_playground):
    entry = local_playground['openai']['/v0/management/auth-files'][1]['files'][0]
    entry['quota']['observed_at'] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat().replace('+00:00', 'Z')
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-overview').click()
            row = page.get_by_test_id('overview-capacity-row').filter(
                has_text='OpenAI Personal')
            row.get_by_text('Stale usage').wait_for()
            assert row.get_by_text('58% used').count() == 0
            page.locator('#metric-fresh').get_by_text('0').wait_for()
            assert errors == []
        finally:
            browser.close()


def test_login_form_submits_with_enter_after_native_validation(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-accounts').click()
            page.get_by_test_id('add-account').click()
            dialog = page.get_by_test_id('login-dialog')
            name = page.get_by_test_id('login-name')
            name.press('Enter')
            assert dialog.is_visible()
            assert page.get_by_test_id('login-start').is_enabled()

            page.get_by_test_id('login-provider').select_option('grok')
            name.fill('Grok Keyboard')
            name.press('Enter')
            dialog.wait_for(state='hidden', timeout=20000)
            page.get_by_test_id('nav-accounts').click()
            page.get_by_test_id('accounts-list').get_by_text('Grok Keyboard').wait_for()
            assert errors == []
        finally:
            browser.close()
