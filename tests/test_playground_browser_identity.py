"""Browser recovery after a managed Claude login observes the wrong email."""

from contextlib import ExitStack
from threading import Thread

import pytest

from agentbridge import Bridge
from playground.server import create_server
from test_managed_account_login import FixtureManagedProxy
from test_playground_browser import _launch_browser, _page, playwright_api
from test_proxy_authentication import (
    FakeProxyGrantBridge, proxy_responses, use_fake_grantbridge,
)
from test_proxy_management import local_management


@pytest.fixture
def mismatched_claude_playground(tmp_path, monkeypatch):
    responses = proxy_responses()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    with ExitStack() as stack:
        port, _ = stack.enter_context(local_management(responses))
        bridge = stack.enter_context(Bridge(tmp_path / 'state'))
        managed = FixtureManagedProxy(port, monkeypatch)
        bridge.managed_proxy = managed
        bridge.authentication.managed_proxy = managed
        bridge.routes.managed_proxy = managed
        use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses, provider='claude'))

        def clear_retired_proxy(_account_id):
            responses['/v0/management/auth-files'] = (200, {'files': []}, {})

        managed.on_retire = clear_retired_proxy
        server = create_server(tmp_path / 'state', port=0, bridge=bridge,
                               workspace_path=workspace)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield {'url': f'http://127.0.0.1:{server.server_port}/',
                   'bridge': bridge, 'managed': managed}
        finally:
            server.shutdown()
            worker.join(timeout=3)
            server.server_close()


def test_wrong_claude_email_closes_failed_login_and_allows_same_name_retry(
        mismatched_claude_playground):
    fixture = mismatched_claude_playground
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, fixture['url'])
            page.get_by_test_id('nav-accounts').click()
            page.get_by_test_id('add-account').click()
            page.get_by_test_id('login-provider').select_option('claude')
            page.get_by_test_id('login-name').fill('Work')
            page.get_by_test_id('login-email').fill('expected@example.test')
            with page.expect_response('**/api/accounts/login/start') as started:
                page.get_by_test_id('login-start').click()
            first = started.value.json()['result']
            page.locator('#login-feedback').get_by_text('identity_changed').wait_for(
                timeout=15000)
            playwright_api.expect(page.get_by_test_id('login-cancel')).to_have_text('Close')
            playwright_api.expect(page.locator('#login-retry-check')).to_be_hidden()
            assert fixture['bridge'].accounts() == []
            assert len(set(fixture['managed'].retires)) == 1

            page.get_by_test_id('login-cancel').click()
            page.get_by_test_id('add-account').click()
            assert page.get_by_test_id('login-name').input_value() == 'Work'
            page.get_by_test_id('login-email').fill('person@example.test')
            with page.expect_response('**/api/accounts/login/start') as restarted:
                page.get_by_test_id('login-start').click()
            second = restarted.value.json()['result']
            assert second['attempt_id'] != first['attempt_id']
            assert second['status'] == 'awaiting_user'
            assert fixture['managed'].provisions == 2
            assert errors == []
        finally:
            browser.close()
