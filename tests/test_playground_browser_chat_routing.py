"""Exercise explicit account choice and automatic affinity in the chat browser."""

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


MODEL = 'fixture/shared-codex-model'


@pytest.fixture
def two_account_chat(tmp_path, monkeypatch):
    command = tmp_path / 'fixture-codex'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    _fake_codex(command)
    with ExitStack() as stack:
        bridge = stack.enter_context(Bridge(tmp_path / 'state'))
        for number, name, used in ((1, 'Codex One', 10), (2, 'Codex Two', 80)):
            responses = _proxy_responses('codex', MODEL, used=used)
            credential = responses['/v0/management/auth-files'][1]['files'][0]
            credential['auth_index'] = f'fixture-codex-{number}'
            credential['id_token']['chatgpt_account_id'] = f'fixture-codex-{number}'
            credential['email'] = f'codex-{number}@example.test'
            port, _ = stack.enter_context(local_management(responses))
            prefix = f'LAB_CHAT_ACCOUNT_{number}'
            monkeypatch.setenv(f'{prefix}_CLIENT_KEY', 'fixture-client')
            monkeypatch.setenv(f'{prefix}_MANAGEMENT_KEY', 'fixture-management')
            account = Account(
                f'codex-{number}', 'codex', name=name, provider='codex',
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
            yield {'url': f'http://127.0.0.1:{server.server_port}/',
                   'bridge': bridge}
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def _choose_model(page):
    page.get_by_test_id('nav-chat').click()
    page.get_by_test_id('chat-provider').select_option('codex')
    page.get_by_test_id('chat-model').select_option(MODEL)


def _send(page, message):
    answers = page.get_by_test_id('chat-messages').get_by_text('Browser fixture answer')
    previous = answers.count()
    page.get_by_test_id('chat-input').fill(message)
    page.get_by_test_id('chat-send').click()
    playwright_api.expect(answers).to_have_count(previous + 1, timeout=15000)
    playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(
        timeout=15000)


def test_blank_automatic_choice_starts_with_least_used_account(two_account_chat):
    bridge = two_account_chat['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, two_account_chat['url'])
            _choose_model(page)
            assert page.get_by_test_id('chat-routing-mode').input_value() == 'automatic'
            assert page.get_by_test_id('chat-account').input_value() == ''
            assert 'least-used' in page.get_by_test_id('chat-account').locator(
                'option').first.inner_text()
            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith('/api/instances')) as created:
                _send(page, 'Choose the least-used account')
            assert created.value.post_data_json['routing_mode'] == 'automatic'
            assert created.value.post_data_json['provider'] == 'codex'
            assert 'account_ref' not in created.value.post_data_json
            instance = bridge.instances()[0]
            assert instance['routing_mode'] == 'automatic'
            assert instance['affinity_account_ref'] == 'Codex One'
            assert 'Codex One' in page.locator('#chat-conversation-subtitle').inner_text()
            assert errors == []
        finally:
            browser.close()


def test_existing_chat_can_change_affinity_pin_and_switch_accounts(two_account_chat):
    bridge = two_account_chat['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, two_account_chat['url'])
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-model').select_option(MODEL)
            mode = page.get_by_test_id('chat-routing-mode')
            account = page.get_by_test_id('chat-account')
            account.select_option('Codex Two')
            assert page.get_by_test_id('chat-provider').input_value() == 'codex'
            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith('/api/instances')) as created:
                _send(page, 'Start with account two')
            assert created.value.post_data_json['routing_mode'] == 'automatic'
            assert created.value.post_data_json['account_ref'] == 'Codex Two'
            instance = bridge.instances()[0]
            instance_id = instance['id']
            assert instance['affinity_account_ref'] == 'Codex Two'
            assert account.input_value() == ''
            assert 'Codex Two' in page.locator('#chat-conversation-subtitle').inner_text()

            _send(page, 'Continue on account two')
            first_turns = bridge.turns(instance_id=instance_id)
            assert len(first_turns) == 2
            assert all(bridge.turn(turn['id'])['account_ref'] == 'Codex Two'
                       for turn in first_turns)

            account.select_option('Codex One')
            assert 'Pending' in page.locator('#route-summary').inner_text()
            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith(f'/api/instances/{instance_id}')) as updated:
                _send(page, 'Prefer account one')
            assert updated.value.post_data_json['routing_mode'] == 'automatic'
            assert updated.value.post_data_json['account_ref'] == 'Codex One'
            assert bridge.instance_get(instance_id)['affinity_account_ref'] == 'Codex One'

            mode.select_option('pinned')
            assert account.input_value() == ''
            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith(f'/api/instances/{instance_id}')) as pinned:
                _send(page, 'Pin the current account')
            assert pinned.value.post_data_json['routing_mode'] == 'pinned'
            assert 'account_ref' not in pinned.value.post_data_json
            assert bridge.instance_get(instance_id)['account_ref'] == 'Codex One'

            account.select_option('Codex Two')
            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith(f'/api/instances/{instance_id}')) as changed:
                _send(page, 'Switch the pinned account')
            assert changed.value.post_data_json['account_ref'] == 'Codex Two'
            assert bridge.instance_get(instance_id)['routing_mode'] == 'pinned'
            assert bridge.instance_get(instance_id)['account_ref'] == 'Codex Two'
            assert 'Pinned to Codex Two' in page.locator(
                '#chat-conversation-subtitle').inner_text()
            assert errors == []
        finally:
            browser.close()
