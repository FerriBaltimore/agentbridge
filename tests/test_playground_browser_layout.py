"""Check playground layout and uncertain data states in a real browser."""

from contextlib import contextmanager
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


def test_responsive_views_dialogs_and_keyboard_navigation(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            for width, height in [(1440, 1000), (390, 844), (320, 700)]:
                page.set_viewport_size({'width': width, 'height': height})
                page.get_by_test_id('nav-overview').click()
                page.get_by_role('heading', name='Available models').wait_for()
                assert page.locator('.view:visible h1').count() == 1
                assert page.get_by_test_id('add-account').is_hidden()
                _assert_layout_fits(page)

                page.get_by_test_id('nav-activity').click()
                page.locator('#activity-title').get_by_text('No conversation selected').wait_for()
                page.get_by_test_id('activity-list').get_by_text('No events recorded').wait_for()
                assert page.locator('.view:visible h1').count() == 1
                assert page.get_by_test_id('add-account').is_hidden()
                _assert_layout_fits(page)

                page.get_by_test_id('nav-accounts').click()
                page.get_by_test_id('accounts-list').get_by_text('OpenAI Personal').wait_for()
                assert page.locator('.view:visible h1').count() == 1
                assert page.get_by_role('button', name='Add account').count() == 1
                _assert_layout_fits(page)

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

                row = page.get_by_test_id('accounts-list').locator('.account-card').filter(
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
            page.locator('#overview-models').get_by_text('No models yet').wait_for()
            page.locator('#overview-usage').get_by_text('No account observations').wait_for()
            assert page.locator('#hero-start-chat').inner_text() == 'Connect an account'
            assert page.locator('#overview-models').get_by_role('button').count() == 0
            assert page.locator('#overview-open-chat').is_hidden()
            assert page.locator('#overview-manage-accounts').is_hidden()
            page.locator('#hero-start-chat').click()
            assert page.locator('#accounts-heading').is_visible()
            for width, height in [(1440, 1000), (390, 844), (320, 700)]:
                page.set_viewport_size({'width': width, 'height': height})
                for view in ['overview', 'accounts', 'chat', 'activity']:
                    page.get_by_test_id(f'nav-{view}').click()
                    assert page.locator(f'#view-{view} h1').is_visible()
                    assert page.locator('.view:visible h1').count() == 1
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
            page.locator('#overview-usage').get_by_text('Unknown usage').wait_for()
            page.get_by_test_id('nav-accounts').click()
            card = page.get_by_test_id('accounts-list').locator('.account-card')
            card.get_by_text('Unknown Signals').wait_for()
            card.get_by_text('Unknown usage').wait_for()
            card.locator('summary').click()
            card.get_by_text('Usage unknown').wait_for()
            assert card.get_by_text('Effort:', exact=False).count() == 0
            assert card.get_by_text('Context:', exact=False).count() == 0

            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-model').select_option('fixture/unknown-metadata')
            assert page.locator('#effort-field').is_hidden()
            assert page.locator('#context-field').is_hidden()
            assert page.locator('#chat-effort').locator('option').all_text_contents() == ['Default']
            assert page.locator('#chat-context').locator('option').all_text_contents() == ['Default']
            page.set_viewport_size({'width': 320, 'height': 700})
            _assert_layout_fits(page)
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
