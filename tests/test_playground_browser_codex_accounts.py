"""Choose between two observed Codex accounts in the Chat route controls."""

from contextlib import ExitStack
from threading import Thread

import pytest

from agentbridge import Account, Bridge
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from playground.server import create_server
from test_playground_browser import (
    _fake_codex, _launch_browser, _page, _proxy_responses, playwright_api,
)
from test_proxy_management import local_management


SHARED_MODEL = 'fixture/codex-shared'
AI1_MODEL = 'fixture/codex-ai1'
AI2_MODEL = 'fixture/codex-ai2'


def _account_responses(account_id, models):
    responses = _proxy_responses('codex', SHARED_MODEL)
    entry = responses['/v0/management/auth-files'][1]['files'][0]
    entry['auth_index'] = f'fixture-{account_id}'
    entry['id_token']['chatgpt_account_id'] = f'fixture-{account_id}'
    responses['/v0/management/auth-files/models?name=one.json'] = (
        200, {'models': [{'id': model} for model in models]}, {})
    responses['/v1/models?client_version=pi'] = (200, {'data': [
        {'slug': model, 'context_window': 131072,
         'supported_reasoning_levels': [{'effort': 'high'}],
         'default_reasoning_level': 'high', 'input_modalities': ['text']}
        for model in models
    ]}, {})
    return responses


@pytest.fixture
def two_codex_playground(tmp_path, monkeypatch):
    command = tmp_path / 'fixture-codex'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    _fake_codex(command)
    with ExitStack() as stack:
        bridge = stack.enter_context(Bridge(tmp_path / 'state'))
        for account_id, models in (
                ('ai1', (SHARED_MODEL, AI1_MODEL)),
                ('ai2', (SHARED_MODEL, AI2_MODEL))):
            responses = _account_responses(account_id, models)
            port, _ = stack.enter_context(local_management(responses))
            key_prefix = f'FIXTURE_{account_id.upper()}'
            monkeypatch.setenv(f'{key_prefix}_CLIENT_KEY', 'fixture-client')
            monkeypatch.setenv(f'{key_prefix}_MANAGEMENT_KEY', 'fixture-management')
            account = Account(
                account_id, 'codex', name=account_id, provider='codex',
                supported_models=models,
                proxy_base_url=f'http://127.0.0.1:{port}/v1',
                key_env=f'{key_prefix}_CLIENT_KEY',
                management_key_env=f'{key_prefix}_MANAGEMENT_KEY',
                command=(str(command),),
            )
            seed_authenticated_proxy_account(bridge.store, account, observe_local=True)
        assert len(bridge.models(refresh=True)['models']) == 3
        server = create_server(tmp_path / 'state', port=0, bridge=bridge,
                               workspace_path=workspace)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield {'url': f'http://127.0.0.1:{server.server_port}/',
                   'bridge': bridge}
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def _option_values(field):
    return field.locator('option').evaluate_all(
        '(options) => options.map((option) => option.value)')


def _send_and_wait(page, message):
    page.get_by_test_id('chat-input').fill(message)
    page.get_by_test_id('chat-send').click()
    page.get_by_test_id('chat-messages').get_by_text(
        'Browser fixture answer').last.wait_for(timeout=15000)
    playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(
        timeout=15000)


def test_codex_account_choice_filters_models_and_pins_conversation(two_codex_playground):
    bridge = two_codex_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, two_codex_playground['url'])
            page.get_by_test_id('nav-chat').click()
            provider = page.get_by_test_id('chat-provider')
            account = page.get_by_test_id('chat-account')
            model = page.get_by_test_id('chat-model')
            assert account.is_visible()

            provider.select_option('codex')
            assert set(_option_values(account)) == {'', 'ai1', 'ai2'}
            page.get_by_test_id('chat-routing-mode').select_option('pinned')
            account.select_option('ai2')
            assert set(_option_values(model)) == {'', SHARED_MODEL, AI2_MODEL}
            account.select_option('ai1')
            assert set(_option_values(model)) == {'', SHARED_MODEL, AI1_MODEL}
            account.select_option('ai2')
            model.select_option(SHARED_MODEL)

            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith('/api/instances')) as created:
                _send_and_wait(page, 'Use ai2')
            assert created.value.post_data_json['account_ref'] == 'ai2'
            assert created.value.post_data_json['routing_mode'] == 'pinned'
            assert 'provider' not in created.value.post_data_json
            instance = bridge.instances()[0]
            assert instance['routing_mode'] == 'pinned'
            assert instance['account_ref'] == 'ai2'
            turn = bridge.turns(instance_id=instance['id'])[0]
            assert bridge.turn(turn['id'])['account_ref'] == 'ai2'

            page.reload(wait_until='networkidle')
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('conversation-item').first.click()
            playwright_api.expect(account).to_have_value('ai2')
            playwright_api.expect(provider).to_have_value('codex')
            playwright_api.expect(model).to_have_value(SHARED_MODEL)
            assert errors == []
        finally:
            browser.close()


def test_existing_automatic_conversation_allows_account_choice(two_codex_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, two_codex_playground['url'])
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-provider').select_option('codex')
            account = page.get_by_test_id('chat-account')
            account.select_option('')
            page.get_by_test_id('chat-model').select_option(SHARED_MODEL)
            _send_and_wait(page, 'Use automatic Codex routing')
            instance = two_codex_playground['bridge'].instances()[0]
            assert instance['routing_mode'] == 'automatic'
            assert instance['routing_provider'] == 'codex'
            assert account.is_enabled()
            help_text = page.locator('#chat-account-help')
            assert help_text.is_visible()
            assert 'confirmed exhaustion' in help_text.inner_text().lower()
            assert 'temporary limits wait' in help_text.inner_text().lower()
            assert page.get_by_role('combobox', name='Account', exact=True).count() == 1
            assert account.get_attribute('aria-describedby') == 'chat-account-help'
            assert errors == []
        finally:
            browser.close()
