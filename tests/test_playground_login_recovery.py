"""Recover uncertain local OAuth starts through the playground SDK boundary."""

import pytest

from agentbridge.errors import BridgeError
from test_playground_browser import _launch_browser, _page, playwright_api
from test_playground_browser_controls import controlled_playground
from test_playground_server import local_server, request


def test_login_attempts_http_route_calls_only_the_public_sdk(local_server):
    server, bridge = local_server
    observed = []

    def attempts(**options):
        observed.append(options)
        return [{'attempt_id': 'fixture-attempt', 'owner_ref': 'fixture-owner',
                 'provider': 'grok', 'account_ref': 'Fixture',
                 'status': 'interrupted', 'error': {'code': 'authentication_outcome_unknown'}}]

    bridge.account_login_attempts = attempts
    status, body = request(server, 'GET', '/api/accounts/login/attempts?provider=grok&limit=3&cursor=2')
    assert status == 200
    assert observed == [{'provider': 'grok', 'limit': 3, 'cursor': 2}]
    assert body['result'][0]['status'] == 'interrupted'


def _raise_unknown(*_):
    raise BridgeError('grantbridge_failed', 'PRIVATE PROVIDER ERROR BODY')


def test_existing_orphan_is_listed_and_only_explicitly_abandoned(controlled_playground,
                                                                   monkeypatch):
    fixture = controlled_playground
    monkeypatch.setattr(fixture['grantbridge'], 'proxy_start', _raise_unknown)
    with pytest.raises(BridgeError) as error:
        fixture['bridge'].account_login_start(provider='grok', name='Orphaned')
    assert error.value.code == 'authentication_outcome_unknown'
    assert len(fixture['bridge'].account_login_attempts()) == 1

    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, fixture['url'])
            page.get_by_test_id('nav-accounts').click()
            page.get_by_test_id('add-account').click()
            recovery = page.locator('#login-recovery')
            recovery.wait_for(state='visible')
            recovery.get_by_text('Orphaned · grok').wait_for()
            assert fixture['bridge'].account_login_attempts()[0]['status'] == 'interrupted'
            recovery.get_by_role('button', name='Abandon Orphaned grok sign-in').click()
            recovery.wait_for(state='hidden')
            assert fixture['bridge'].account_login_attempts() == []
            assert fixture['grantbridge'].starts == 0
            assert errors == []
        finally:
            browser.close()


def test_unknown_start_exposes_abandon_without_automatic_retry(controlled_playground,
                                                                  monkeypatch):
    fixture = controlled_playground
    monkeypatch.setattr(fixture['grantbridge'], 'proxy_start', _raise_unknown)
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, fixture['url'])
            page.get_by_test_id('nav-accounts').click()
            page.get_by_test_id('add-account').click()
            page.get_by_test_id('login-provider').select_option('grok')
            page.get_by_test_id('login-name').fill('New uncertain account')
            page.get_by_test_id('login-start').click()
            page.locator('#login-feedback').get_by_text('authentication_outcome_unknown').wait_for()
            page.get_by_test_id('login-status').get_by_text('could not be confirmed').wait_for()
            assert 'PRIVATE PROVIDER ERROR BODY' not in page.locator('body').inner_text()
            assert len(fixture['bridge'].account_login_attempts()) == 1
            page.get_by_test_id('login-cancel').get_by_text('Abandon attempt').click()
            page.get_by_test_id('login-status').get_by_text('attempt was closed').wait_for()
            assert fixture['bridge'].account_login_attempts() == []
            assert fixture['grantbridge'].starts == 0
            assert errors == []
        finally:
            browser.close()


def test_callback_port_conflict_stays_actionable_in_form(controlled_playground, monkeypatch):
    fixture = controlled_playground

    def busy(*_):
        raise BridgeError('oauth_callback_port_busy',
                          'Local OAuth callback port 1455 is in use. Close the application using it.')

    monkeypatch.setattr(fixture['grantbridge'], 'proxy_start', busy)
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, fixture['url'])
            page.get_by_test_id('nav-accounts').click()
            page.get_by_test_id('add-account').click()
            page.get_by_test_id('login-provider').select_option('grok')
            page.get_by_test_id('login-name').fill('Busy port')
            page.get_by_test_id('login-start').click()
            page.locator('#login-feedback').get_by_text('oauth_callback_port_busy').wait_for()
            assert page.locator('#login-form').is_visible()
            assert fixture['bridge'].account_login_attempts() == []
            assert errors == []
        finally:
            browser.close()
