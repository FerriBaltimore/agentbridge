"""Exercise in-place chat routing changes through the browser and public SDK."""

from contextlib import ExitStack
import json
from threading import Thread

import pytest

from agentbridge import Account, Bridge
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from playground.server import create_server
from test_playground_browser import (
    NativeCapture, _fake_codex, _launch_browser, _page, _proxy_responses,
    playwright_api,
)
from test_proxy_management import local_management


OPENAI_MODEL = 'fixture/reconfigure-openai'
CLAUDE_MODEL = 'fixture/reconfigure-claude'


@pytest.fixture
def route_change_playground(tmp_path, monkeypatch):
    command = tmp_path / 'fixture-codex'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    _fake_codex(command)
    capture = NativeCapture(tmp_path / 'state')
    accounts = (
        ('codex', 'OpenAI Personal', OPENAI_MODEL, 131072, 'high', 'OPENAI'),
        ('claude', 'Claude Research', CLAUDE_MODEL, 65536, 'low', 'CLAUDE'),
    )
    with ExitStack() as stack:
        bridge = stack.enter_context(Bridge(tmp_path / 'state'))
        for provider, name, model, window, effort, prefix in accounts:
            responses = _proxy_responses(provider, model)
            responses['/v1/models?client_version=pi'] = (200, {'data': [{
                'slug': model, 'context_window': window,
                'supported_reasoning_levels': [{'effort': effort}],
                'default_reasoning_level': effort, 'input_modalities': ['text'],
            }]}, {})
            port, _ = stack.enter_context(local_management(responses))
            monkeypatch.setenv(f'LAB_ROUTE_{prefix}_CLIENT_KEY', 'fixture-client')
            monkeypatch.setenv(f'LAB_ROUTE_{prefix}_MANAGEMENT_KEY', 'fixture-management')
            account = Account(
                provider, 'codex', name=name, provider=provider,
                supported_models=(model,),
                proxy_base_url=f'http://127.0.0.1:{port}/v1',
                key_env=f'LAB_ROUTE_{prefix}_CLIENT_KEY',
                management_key_env=f'LAB_ROUTE_{prefix}_MANAGEMENT_KEY',
                command=(str(command),),
            )
            seed_authenticated_proxy_account(bridge.store, account, observe_local=True)
        assert len(bridge.models(refresh=True)['models']) == 2
        server = create_server(tmp_path / 'state', port=0, bridge=bridge,
                               workspace_path=workspace)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield {'url': f'http://127.0.0.1:{server.server_port}/',
                   'bridge': bridge, 'capture': capture}
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def _send_and_wait(page, message):
    page.get_by_test_id('chat-input').fill(message)
    page.get_by_test_id('chat-send').click()
    page.get_by_test_id('chat-messages').get_by_text(
        'Browser fixture answer').last.wait_for(timeout=15000)
    playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(
        timeout=15000)


def _native_calls(capture):
    return [json.loads(line) for line in capture.read_text().splitlines()]


def test_pinned_chat_can_switch_account_provider_model_effort_and_context_in_place(
        route_change_playground):
    bridge = route_change_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, route_change_playground['url'])
            page.get_by_test_id('nav-chat').click()
            provider = page.get_by_test_id('chat-provider')
            model = page.get_by_test_id('chat-model')
            account = page.get_by_test_id('chat-account')
            effort = page.get_by_test_id('chat-effort')
            context = page.get_by_test_id('chat-context')

            provider.select_option('codex')
            model.select_option(OPENAI_MODEL)
            page.locator('#route-settings summary').click()
            page.get_by_test_id('chat-routing-mode').select_option('pinned')
            account.select_option('OpenAI Personal')
            effort.select_option('high')
            assert context.locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)') == ['', '131072']
            context.select_option('131072')
            _send_and_wait(page, 'First route')
            first = bridge.instances()[0]
            instance_id = first['id']
            assert first['routing_mode'] == 'pinned'
            assert bridge.turn(bridge.turns(instance_id=instance_id)[0]['id'])[
                'account_ref'] == 'OpenAI Personal'

            playwright_api.expect(provider).to_be_enabled()
            provider.select_option('claude')
            assert CLAUDE_MODEL in model.locator('option').all_text_contents()
            model.select_option(CLAUDE_MODEL)
            assert account.input_value() == ''
            account.select_option('Claude Research')
            assert effort.locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)') == ['', 'low']
            effort.select_option('low')
            assert context.locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)') == ['', '65536']
            assert context.input_value() == ''
            assert len(bridge.turns(instance_id=instance_id)) == 1
            context.select_option('65536')
            playwright_api.expect(page.get_by_test_id('chat-send')).to_be_enabled()
            assert 'Pending' in page.locator('#route-summary').inner_text()
            assert bridge.instance_get(instance_id)['model'] == OPENAI_MODEL
            assert bridge.instance_get(instance_id)['routing_mode'] == 'pinned'

            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith(f'/api/instances/{instance_id}')) as updated:
                _send_and_wait(page, 'Second route')
            assert updated.value.post_data_json == {
                'expected_version': first['version'],
                'model': CLAUDE_MODEL,
                'account_ref': 'Claude Research',
            }
            assert len(bridge.instances()) == 1
            changed = bridge.instance_get(instance_id)
            assert changed['routing_mode'] == 'pinned'
            assert changed['native_session_id'] == first['native_session_id']
            assert changed['account_ref'] == 'Claude Research'
            assert changed['model'] == CLAUDE_MODEL
            assert changed['version'] == first['version'] + 1
            turns = bridge.turns(instance_id=instance_id)
            assert len(turns) == 2
            assert [bridge.turn(turn['id'])['account_ref'] for turn in turns] == [
                'OpenAI Personal', 'Claude Research']
            calls = _native_calls(route_change_playground['capture'])
            assert len(calls) == 2
            first_call = next(call for call in calls if OPENAI_MODEL in call['argv'])
            second_call = next(call for call in calls if CLAUDE_MODEL in call['argv'])
            assert 'resume' not in first_call['argv']
            resume_index = second_call['argv'].index('resume')
            assert second_call['argv'][resume_index + 1] == first['native_session_id']
            assert second_call['prompt'] == 'Second route'
            assert 'model_context_window=131072' in first_call['argv']
            assert 'model_reasoning_effort="high"' in first_call['argv']
            assert 'model_context_window=65536' in second_call['argv']
            assert 'model_reasoning_effort="low"' in second_call['argv']
            assert 'Pending' not in page.locator('#route-summary').inner_text()
            page.reload(wait_until='networkidle')
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('conversation-item').first.click()
            playwright_api.expect(page.get_by_test_id('chat-provider')).to_have_value('claude')
            playwright_api.expect(page.get_by_test_id('chat-model')).to_have_value(CLAUDE_MODEL)
            assert page.get_by_test_id('chat-messages').get_by_text('First route').count() == 1
            assert page.get_by_test_id('chat-messages').get_by_text('Second route').count() == 1
            assert errors == []
        finally:
            browser.close()


def test_failed_route_update_keeps_draft_and_does_not_send(route_change_playground):
    bridge = route_change_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, route_change_playground['url'])
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-provider').select_option('codex')
            page.get_by_test_id('chat-model').select_option(OPENAI_MODEL)
            _send_and_wait(page, 'Initial message')
            first = bridge.instances()[0]
            instance_id = first['id']
            page.get_by_test_id('chat-provider').select_option('claude')
            page.get_by_test_id('chat-model').select_option(CLAUDE_MODEL)
            page.get_by_test_id('chat-input').fill('Draft after failed update')
            target = f'**/api/instances/{instance_id}'

            def reject_update(route):
                if route.request.method == 'POST':
                    route.fulfill(status=409, content_type='application/json',
                                  body=json.dumps({'error': {'code': 'version_conflict',
                                                             'message': 'Fixture version conflict.'}}))
                else:
                    route.continue_()

            page.route(target, reject_update)
            page.get_by_test_id('chat-send').click()
            page.locator('#toast-region').get_by_text('Fixture version conflict.').wait_for()
            assert page.get_by_test_id('chat-input').input_value() == 'Draft after failed update'
            assert bridge.instance_get(instance_id)['model'] == OPENAI_MODEL
            assert len(bridge.turns(instance_id=instance_id)) == 1
            assert len(_native_calls(route_change_playground['capture'])) == 1
            page.unroute(target, reject_update)
            _send_and_wait(page, 'Draft after failed update')
            assert len(bridge.instances()) == 1
            assert bridge.instance_get(instance_id)['model'] == CLAUDE_MODEL
            assert len(bridge.turns(instance_id=instance_id)) == 2
            assert errors == []
        finally:
            browser.close()


def test_automatic_chat_changes_provider_and_model_without_new_instance(
        route_change_playground):
    bridge = route_change_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, route_change_playground['url'])
            page.get_by_test_id('nav-chat').click()
            provider = page.get_by_test_id('chat-provider')
            model = page.get_by_test_id('chat-model')
            account = page.get_by_test_id('chat-account')
            provider.select_option('codex')
            model.select_option(OPENAI_MODEL)
            assert account.input_value() == ''
            _send_and_wait(page, 'Automatic OpenAI turn')
            first = bridge.instances()[0]
            instance_id = first['id']
            assert first['routing_mode'] == 'automatic'
            assert first['routing_provider'] == 'codex'
            assert account.is_enabled()

            provider.select_option('claude')
            model.select_option(CLAUDE_MODEL)
            assert 'Pending' in page.locator('#route-summary').inner_text()
            assert 'least-used eligible account' in account.locator('option').first.inner_text()
            assert 'least-used eligible account' in page.locator('#route-summary').inner_text()
            assert bridge.instance_get(instance_id)['model'] == OPENAI_MODEL
            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith(f'/api/instances/{instance_id}')) as updated:
                _send_and_wait(page, 'Automatic Claude turn')
            assert updated.value.post_data_json == {
                'expected_version': first['version'], 'model': CLAUDE_MODEL,
                'provider': 'claude',
            }
            assert len(bridge.instances()) == 1
            changed = bridge.instance_get(instance_id)
            assert changed['routing_mode'] == 'automatic'
            assert changed['native_session_id'] == first['native_session_id']
            assert changed['routing_provider'] == 'claude'
            assert changed['model'] == CLAUDE_MODEL
            turns = bridge.turns(instance_id=instance_id)
            assert len(turns) == 2
            assert [bridge.turn(turn['id'])['account_ref'] for turn in turns] == [
                'OpenAI Personal', 'Claude Research']
            calls = _native_calls(route_change_playground['capture'])
            assert len(calls) == 2
            resume_index = calls[1]['argv'].index('resume')
            assert calls[1]['argv'][resume_index + 1] == first['native_session_id']
            assert calls[1]['prompt'] == 'Automatic Claude turn'
            assert errors == []
        finally:
            browser.close()
