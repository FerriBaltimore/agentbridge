"""Exercise playground controls against the SDK with local provider fixtures."""

from contextlib import ExitStack
import json
from pathlib import Path
from threading import Thread
import textwrap
import time

import pytest

from agentbridge import Bridge
from playground.server import create_server
from test_playground_browser import (
    _launch_browser, _page, _proxy_responses, configure_fixture_login,
    local_playground, playwright_api,
)
from test_proxy_management import local_management


class ControllableGrantBridge:
    def __init__(self):
        self.status = 'awaiting_user'
        self.starts = 0
        self.cancels = 0

    def configuration(self):
        return {'adapter': 'fixture', 'data_dir': None, 'node': 'fixture'}

    def proxy_start(self, provider, base_url, management_key_env):
        assert provider == 'grok' and base_url.endswith('/v1')
        assert management_key_env == 'LAB_GROK_MANAGEMENT_KEY'
        self.starts += 1
        return {'id': f'fixture-oauth-{self.starts}', 'provider': provider,
                'status': 'awaiting_user',
                'authorizationUrl': 'https://auth.example.test/authorize',
                'userCode': 'ABCD-EFGH'}

    def proxy_status(self, state, provider, base_url, management_key_env):
        assert state.startswith('fixture-oauth-') and provider == 'grok'
        return {'id': state, 'provider': provider, 'status': self.status}

    def proxy_cancel(self, state, provider, base_url, management_key_env):
        self.cancels += 1
        return {'id': state, 'provider': provider, 'status': 'cancelled'}

    def close(self):
        pass


@pytest.fixture
def controlled_playground(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setenv('LAB_GROK_CLIENT_KEY', 'fixture-client')
    monkeypatch.setenv('LAB_GROK_MANAGEMENT_KEY', 'fixture-management')
    responses = _proxy_responses('grok', 'fixture/grok-model', credential=False)
    with ExitStack() as stack:
        port, _ = stack.enter_context(local_management(responses))
        bridge = stack.enter_context(Bridge(tmp_path / 'state'))
        grantbridge = ControllableGrantBridge()
        monkeypatch.setattr('agentbridge.authentication.GrantBridgeClient',
                            lambda *args, **kwargs: grantbridge)
        managed = configure_fixture_login(bridge, monkeypatch, port)
        server = create_server(tmp_path / 'state', port=0, bridge=bridge,
                               workspace_path=workspace)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield {'url': f'http://127.0.0.1:{server.server_port}/',
                   'bridge': bridge, 'grantbridge': grantbridge,
                   'responses': responses, 'port': port, 'managed': managed}
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def _fill_login(page, *, name='Grok Lab'):
    page.get_by_test_id('nav-accounts').click()
    page.get_by_test_id('add-account').click()
    page.get_by_test_id('login-provider').select_option('grok')
    page.get_by_test_id('login-name').fill(name)


def test_codex_catalog_models_shape_exposes_chat_effort_and_context(local_playground):
    """The browser consumes controls observed from CLIProxyAPI's Codex payload."""
    local_playground['openai']['/v1/models?client_version=pi'] = (200, {'models': [
        {'slug': 'fixture/openai-model', 'context_window': 131072,
         'max_context_window': 262144,
         'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'high'}],
         'default_reasoning_level': 'high'},
    ]}, {})
    catalog = local_playground['bridge'].models(account_ref='OpenAI Personal', refresh=True)
    assert catalog['items'][0]['reasoning_efforts'] == ['low', 'high']
    assert catalog['items'][0]['context_windows'] == [131072, 262144]
    assert catalog['items'][0]['account_capabilities'][0]['metadata_source'] == 'cliproxy_client_models'

    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-provider').select_option('codex')
            page.get_by_test_id('chat-model').select_option('fixture/openai-model')
            effort = page.get_by_test_id('chat-effort')
            effort.wait_for(state='visible')
            assert effort.locator('option').all_text_contents() == ['Default', 'low', 'high']
            effort.select_option('high')
            context = page.get_by_test_id('chat-context')
            context.wait_for(state='visible')
            assert context.evaluate('(element) => element.tagName') == 'SELECT'
            assert context.locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)') == ['', '131072', '262144']
            assert context.locator('option').all_text_contents() == [
                'Provider default · 131,072 tokens', '131,072 tokens', '262,144 tokens']
            page.get_by_test_id('chat-input').fill('Test observed Codex controls')
            assert '262145' not in context.locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)')
            context.select_option('262144')
            playwright_api.expect(page.get_by_test_id('chat-send')).to_be_enabled()
            page.get_by_test_id('chat-send').click()
            page.get_by_test_id('chat-messages').get_by_text('Browser fixture answer').wait_for(
                timeout=15000)
            calls = [json.loads(line) for line in local_playground['capture'].read_text().splitlines()]
            assert len(calls) == 1
            assert 'model_context_window=262144' in calls[0]['argv']
            assert 'model_reasoning_effort="high"' in calls[0]['argv']
            assert errors == []
        finally:
            browser.close()


def test_oauth_validation_pending_reopen_and_confirmed_cancel(controlled_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, controlled_playground['url'])
            grantbridge = controlled_playground['grantbridge']
            _fill_login(page, name='   ')
            page.get_by_test_id('login-start').click()
            page.locator('#login-feedback').get_by_text('invalid_name').wait_for()
            assert grantbridge.starts == 0

            page.get_by_test_id('login-name').fill('Grok Lab')
            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith('/api/accounts/login/start')) as sent:
                page.get_by_test_id('login-start').click()
            assert sent.value.post_data_json == {'provider': 'grok', 'name': 'Grok Lab'}
            page.get_by_test_id('login-status').get_by_text(
                'Waiting for authorization in your browser').wait_for()
            assert len(controlled_playground['managed'].provisioned) == 1
            assert page.get_by_test_id('login-open-url').get_attribute('href') == (
                'https://auth.example.test/authorize')
            page.locator('#login-user-code').get_by_text('ABCD-EFGH').wait_for()
            page.context.route('https://auth.example.test/**', lambda route: route.fulfill(
                status=200, content_type='text/html', body='<title>Fixture OAuth</title>'))
            with page.expect_popup() as opened:
                page.get_by_test_id('login-open-url').click()
            popup = opened.value
            popup.wait_for_load_state()
            assert popup.url == 'https://auth.example.test/authorize'
            popup.close()
            page.get_by_role('button', name='Close dialog').click()
            assert not page.get_by_test_id('login-dialog').is_visible()
            page.get_by_test_id('add-account').click()
            page.get_by_test_id('login-status').get_by_text(
                'Waiting for authorization in your browser').wait_for()
            assert grantbridge.starts == 1

            page.get_by_test_id('login-cancel').click()
            page.get_by_test_id('login-status').get_by_text(
                'Sign-in was cancelled.').wait_for()
            assert grantbridge.cancels == 1
            assert controlled_playground['bridge'].accounts() == []
            assert len(controlled_playground['managed'].retired) == 1
            page.get_by_test_id('login-cancel').click()
            page.get_by_test_id('add-account').click()
            assert page.get_by_test_id('login-name').is_visible()
            assert page.get_by_test_id('login-provider').input_value() == 'grok'
            page.get_by_test_id('login-start').click()
            page.get_by_test_id('login-status').get_by_text(
                'Waiting for authorization in your browser').wait_for()
            assert grantbridge.starts == 2
            assert errors == []
        finally:
            browser.close()


def test_oauth_verification_failure_can_retry_after_proxy_recovers(controlled_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, controlled_playground['url'])
            _fill_login(page)
            page.get_by_test_id('login-start').click()
            page.get_by_test_id('login-status').get_by_text(
                'Waiting for authorization in your browser').wait_for()
            controlled_playground['grantbridge'].status = 'authorized'
            page.locator('#login-retry-check').wait_for(state='visible', timeout=10000)
            assert page.locator('#login-feedback').is_visible()
            assert controlled_playground['bridge'].accounts() == []

            active = _proxy_responses('grok', 'fixture/grok-model')
            controlled_playground['responses']['/v0/management/auth-files'] = (
                active['/v0/management/auth-files'])
            page.locator('#login-retry-check').click()
            page.get_by_test_id('nav-accounts').click()
            page.get_by_test_id('accounts-list').get_by_text('Grok Lab').wait_for(
                timeout=10000)
            assert len(controlled_playground['bridge'].accounts()) == 1
            assert 'never-render-this' not in page.locator('body').inner_text()
            assert errors == []
        finally:
            browser.close()


def test_pinned_conversations_new_chat_navigation_and_refresh(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-chat').click()
            assert page.locator('#view-chat h1').inner_text() == 'Chat'
            page.get_by_test_id('chat-provider').select_option('codex')
            page.get_by_test_id('chat-model').select_option('fixture/openai-model')
            page.locator('#route-settings summary').click()
            route = page.get_by_test_id('chat-account')
            assert route.locator('option').count() == 2
            route.select_option('OpenAI Personal')
            page.get_by_test_id('chat-input').fill('Pinned OpenAI request')
            page.get_by_test_id('chat-send').click()
            page.get_by_test_id('chat-messages').get_by_text('Browser fixture answer').wait_for(
                timeout=15000)
            playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(
                timeout=15000)
            assert page.get_by_test_id('chat-provider').is_enabled()
            assert route.is_enabled() and route.input_value() == 'OpenAI Personal'
            assert page.get_by_test_id('conversation-list').get_by_test_id(
                'conversation-item').count() == 1

            page.get_by_test_id('nav-activity').click()
            assert page.get_by_test_id('activity-list').locator('.event-item').count() > 0
            page.locator('#activity-refresh').click()
            page.get_by_test_id('nav-accounts').click()
            row = page.get_by_test_id('accounts-list').get_by_test_id('account-row').filter(
                has_text='Claude Research')
            row.get_by_test_id('remove-account').click()
            page.locator('#remove-cancel').click()
            row.get_by_text('Claude Research').wait_for()
            page.locator('#refresh-button').click()
            playwright_api.expect(page.locator('#refresh-button')).to_be_enabled()
            page.get_by_test_id('nav-chat').click()
            assert page.get_by_test_id('chat-model').input_value() == 'fixture/openai-model'

            page.locator('#new-conversation').click()
            assert page.get_by_test_id('chat-provider').is_enabled()
            assert page.get_by_test_id('chat-provider').input_value() == ''
            page.get_by_test_id('chat-provider').select_option('claude')
            assert 'fixture/openai-model' not in page.get_by_test_id('chat-model').locator(
                'option').all_text_contents()
            page.get_by_test_id('chat-model').select_option('fixture/claude-model')
            page.locator('#route-settings summary').click()
            route.select_option('Claude Research')
            page.get_by_test_id('chat-input').fill('Pinned Claude request')
            page.get_by_test_id('chat-send').click()
            page.get_by_test_id('chat-messages').get_by_text('Browser fixture answer').wait_for(
                timeout=15000)
            playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(
                timeout=15000)
            assert page.get_by_test_id('conversation-list').get_by_test_id(
                'conversation-item').count() == 2
            first = page.get_by_test_id('conversation-item').filter(
                has_text='fixture/openai-model')
            first.click()
            playwright_api.expect(page.get_by_test_id('chat-model')).to_have_value(
                'fixture/openai-model')
            playwright_api.expect(route).to_have_value('OpenAI Personal')
            page.get_by_test_id('chat-messages').get_by_text(
                'Pinned OpenAI request').wait_for()
            calls = [json.loads(line) for line in local_playground['capture'].read_text().splitlines()]
            assert len(calls) == 2
            assert all(model in str(call['argv']) for model, call in zip(
                ('fixture/openai-model', 'fixture/claude-model'), calls))
            page.reload(wait_until='networkidle')
            page.get_by_test_id('nav-chat').click()
            assert page.get_by_test_id('conversation-item').count() == 2
            page.get_by_test_id('conversation-item').filter(
                has_text='fixture/openai-model').click()
            page.get_by_test_id('chat-messages').get_by_text(
                'Pinned OpenAI request').wait_for()
            assert errors == []
        finally:
            browser.close()


def test_send_failure_keeps_message_for_safe_retry(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-provider').select_option('codex')
            page.get_by_test_id('chat-model').select_option('fixture/openai-model')
            blocked = 0

            def fail_once(route):
                nonlocal blocked
                if route.request.method == 'POST' and blocked == 0:
                    blocked += 1
                    route.fulfill(status=503, content_type='application/json',
                                  body=json.dumps({'error': {'code': 'fixture_outage',
                                                            'message': 'Fixture send unavailable.'}}))
                else:
                    route.continue_()

            page.route('**/api/instances/*/messages', fail_once)
            page.get_by_test_id('chat-input').fill('Please retry this request')
            page.get_by_test_id('chat-send').click()
            page.locator('#toast-region').get_by_text('Fixture send unavailable.').wait_for()
            assert blocked == 1
            assert page.get_by_test_id('chat-input').input_value() == 'Please retry this request'
            assert not local_playground['capture'].exists()
            page.unroute('**/api/instances/*/messages', fail_once)
            page.get_by_test_id('chat-send').click()
            page.get_by_test_id('chat-messages').get_by_text('Browser fixture answer').wait_for(
                timeout=15000)
            calls = [json.loads(line) for line in local_playground['capture'].read_text().splitlines()]
            assert len(calls) == 1 and 'Please retry this request' in calls[0]['prompt']
            assert errors == []
        finally:
            browser.close()


def test_stop_button_cancels_one_running_turn_without_rerouting(local_playground):
    executable = Path(local_playground['bridge'].accounts()[0].command[0])
    capture = local_playground['capture']
    executable.write_text(textwrap.dedent('''\
        #!/usr/bin/python3
        import json
        import os
        from pathlib import Path
        import sys
        import time

        if '--version' in sys.argv:
            print('codex-cli 0.0.0')
            sys.exit(0)
        prompt = sys.stdin.read()
        capture = Path(os.environ['CODEX_HOME']) / 'fixture-calls.jsonl'
        with capture.open('a') as stream:
            stream.write(json.dumps({'argv': sys.argv[1:], 'prompt': prompt}) + '\\n')
        print(json.dumps({'type': 'thread.started', 'thread_id': 'fixture-slow'}),
              flush=True)
        time.sleep(30)
    '''))
    executable.chmod(0o700)
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-provider').select_option('codex')
            page.get_by_test_id('chat-model').select_option('fixture/openai-model')
            page.get_by_test_id('chat-input').fill('Stop this running fixture')
            page.get_by_test_id('chat-send').click()
            page.get_by_test_id('chat-stop').wait_for(state='visible', timeout=10000)
            playwright_api.expect(page.locator('#new-conversation')).to_be_disabled()
            deadline = time.monotonic() + 5
            while not capture.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert capture.exists(), 'The fixture worker did not start before Stop.'
            page.get_by_test_id('chat-stop').click()
            page.locator('#toast-region').get_by_text('Stop requested for this turn.').wait_for()
            playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(
                timeout=15000)
            page.get_by_test_id('nav-activity').click()
            page.get_by_test_id('activity-list').get_by_text('State: cancelled').wait_for()
            assert len(capture.read_text().splitlines()) == 1
            assert errors == []
        finally:
            browser.close()


def test_refresh_reports_api_failure_and_recovers(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])

            def unavailable(route):
                route.fulfill(status=503, content_type='application/json',
                              body=json.dumps({'error': {
                                  'code': 'fixture_unavailable',
                                  'message': 'Fixture capabilities unavailable.'}}))

            page.route('**/api/capabilities', unavailable)
            page.locator('#refresh-button').click()
            page.locator('#global-error').get_by_text(
                'Fixture capabilities unavailable.').wait_for()
            assert page.locator('#metric-accounts').inner_text() == '2'
            page.unroute('**/api/capabilities', unavailable)
            page.locator('#refresh-button').click()
            playwright_api.expect(page.locator('#global-error')).to_be_hidden()
            page.get_by_test_id('nav-chat').click()
            assert 'codex' in page.get_by_test_id('chat-provider').locator('option').evaluate_all(
                '(options) => options.map((option) => option.value)')
            assert errors == []
        finally:
            browser.close()
