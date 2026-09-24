"""Browser provider choices constrain automatic routing for a shared model."""

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


MODEL = 'fixture/shared-model'


@pytest.fixture
def shared_model_playground(tmp_path, monkeypatch):
    command = tmp_path / 'fixture-codex'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    _fake_codex(command)
    descriptions = (
        ('codex', 'OpenAI Route', 131072, 'high', 'LAB_SHARED_OPENAI'),
        ('claude', 'Claude Route', 65536, 'low', 'LAB_SHARED_CLAUDE'),
    )
    with ExitStack() as stack:
        bridge = stack.enter_context(Bridge(tmp_path / 'state'))
        for provider, name, window, effort, prefix in descriptions:
            responses = _proxy_responses(provider, MODEL, used=20 if provider == 'codex' else None)
            responses['/v1/models?client_version=pi'] = (200, {'data': [{
                'slug': MODEL, 'context_window': window,
                'supported_reasoning_levels': [{'effort': effort}],
                'default_reasoning_level': effort, 'input_modalities': ['text'],
            }]}, {})
            port, _ = stack.enter_context(local_management(responses))
            monkeypatch.setenv(f'{prefix}_CLIENT_KEY', f'fixture-{provider}-client')
            monkeypatch.setenv(f'{prefix}_MANAGEMENT_KEY', f'fixture-{provider}-management')
            account = Account(
                provider, 'codex', name=name, provider=provider,
                supported_models=(MODEL,),
                proxy_base_url=f'http://127.0.0.1:{port}/v1',
                key_env=f'{prefix}_CLIENT_KEY',
                management_key_env=f'{prefix}_MANAGEMENT_KEY',
                command=(str(command),),
            )
            seed_authenticated_proxy_account(bridge.store, account, observe_local=True)
        assert len(bridge.models(refresh=True)['models']) == 1
        server = create_server(tmp_path / 'state', port=0, bridge=bridge,
                               workspace_path=workspace)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield {'url': f'http://127.0.0.1:{server.server_port}/', 'bridge': bridge}
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def test_browser_provider_filters_shared_model_controls_and_route(shared_model_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, shared_model_playground['url'])
            page.get_by_test_id('nav-chat').click()
            model = page.get_by_test_id('chat-model')
            provider = page.get_by_test_id('chat-provider')
            model.select_option(MODEL)
            assert page.locator('#effort-field').is_hidden()
            assert page.get_by_test_id('chat-context').locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)') == ['', '65536']

            provider.select_option('claude')
            assert model.input_value() == MODEL
            assert page.get_by_test_id('chat-effort').locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)') == ['', 'low']
            assert page.get_by_test_id('chat-context').locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)') == ['', '65536']
            page.get_by_test_id('chat-input').fill('Route within Claude')
            page.get_by_test_id('chat-send').click()
            page.get_by_test_id('chat-messages').get_by_text('Browser fixture answer').wait_for(
                timeout=15000)
            playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(
                timeout=15000)
            bridge = shared_model_playground['bridge']
            first = bridge.instances()[0]
            assert first['routing_provider'] == 'claude'
            first_turn = bridge.turns(instance_id=first['id'])[0]
            assert bridge.turn(first_turn['id'])['account_ref'] == 'Claude Route'

            page.locator('#new-conversation').click()
            provider.select_option('codex')
            model.select_option(MODEL)
            assert page.get_by_test_id('chat-effort').locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)') == ['', 'high']
            assert page.get_by_test_id('chat-context').locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)') == ['', '131072']
            page.get_by_test_id('chat-input').fill('Route within OpenAI')
            page.get_by_test_id('chat-send').click()
            page.get_by_test_id('chat-messages').get_by_text('Browser fixture answer').wait_for(
                timeout=15000)
            playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(
                timeout=15000)
            second = next(item for item in bridge.instances() if item['id'] != first['id'])
            assert second['routing_provider'] == 'codex'
            second_turn = bridge.turns(instance_id=second['id'])[0]
            assert bridge.turn(second_turn['id'])['account_ref'] == 'OpenAI Route'
            assert errors == []
        finally:
            browser.close()
