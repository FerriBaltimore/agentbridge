"""Saved access controls remain independent of the chat's native Codex session."""

import json
from pathlib import Path

import pytest

from test_playground_browser import _launch_browser, _page, playwright_api
from test_playground_browser_reconfiguration import (
    CLAUDE_MODEL, OPENAI_MODEL, route_change_playground,
)


@pytest.fixture
def access_playground(route_change_playground):
    fixture = Path(__file__).parent / 'fixtures/test_playground_access_provider.py'
    bridge = route_change_playground['bridge']
    command = Path(bridge.accounts()[0].command[0])
    command.write_text('#!/usr/bin/python3\n' + fixture.read_text())
    yield route_change_playground


def settings(page):
    panel = page.locator('#route-settings')
    if not panel.evaluate('(element) => element.open'):
        panel.locator('summary').click()
    return page.get_by_test_id('chat-sandbox'), page.get_by_test_id('chat-permission')


def send(page, prompt, count):
    page.get_by_test_id('chat-input').fill(prompt)
    with page.expect_request(lambda request: request.method == 'POST'
                             and request.url.endswith('/messages')) as sent:
        page.get_by_test_id('chat-send').click()
    assert 'permission_mode' not in sent.value.post_data_json
    assert 'sandbox_mode' not in sent.value.post_data_json
    playwright_api.expect(page.get_by_test_id('chat-messages').get_by_text(
        'Access fixture answer', exact=True)).to_have_count(count, timeout=15000)
    playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(timeout=15000)


def test_access_create_save_reload_and_route_change_keep_one_native_session(access_playground, tmp_path):
    bridge = access_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, access_playground['url'])
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-provider').select_option('codex')
            page.get_by_test_id('chat-model').select_option(OPENAI_MODEL)
            sandbox, permission = settings(page)
            assert sandbox.input_value() == 'read-only'
            assert permission.input_value() == 'dontAsk'
            assert sandbox.locator('option').all_text_contents() == [
                'Read-only', 'Workspace write', 'Full access']
            sandbox.select_option('danger-full-access')
            assert 'host file and network access' in page.locator('#access-help').inner_text()
            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith('/api/instances')) as created:
                send(page, 'First full access request', 1)
            assert created.value.post_data_json['sandbox_mode'] == 'danger-full-access'
            assert created.value.post_data_json['permission_mode'] == 'dontAsk'
            first = bridge.instances()[0]
            instance_id = first['id']
            native_id = first['native_session_id']
            assert native_id == 'fixture-access-session'

            sandbox, permission = settings(page)
            sandbox.select_option('workspace-write')
            permission.select_option('default')
            with page.expect_response(lambda response: response.request.method == 'POST'
                                      and response.url.endswith(f'/api/instances/{instance_id}')) as saved:
                page.get_by_test_id('save-access-settings').click()
            assert saved.value.ok
            assert saved.value.request.post_data_json == {'expected_version': first['version'],
                'sandbox_mode': 'workspace-write', 'permission_mode': 'default'}
            playwright_api.expect(page.get_by_test_id('save-access-settings')).to_be_disabled()
            assert len(bridge.turns(instance_id=instance_id)) == 1
            assert bridge.instance_get(instance_id)['native_session_id'] == native_id

            page.reload(wait_until='networkidle')
            page.get_by_test_id('nav-chat').click()
            playwright_api.expect(page.get_by_test_id('chat-model')).to_have_value(OPENAI_MODEL)
            sandbox, permission = settings(page)
            assert sandbox.input_value() == 'workspace-write'
            assert permission.input_value() == 'default'
            send(page, 'Second restricted request', 2)

            page.get_by_test_id('chat-provider').select_option('claude')
            page.get_by_test_id('chat-model').select_option(CLAUDE_MODEL)
            page.get_by_test_id('chat-routing-mode').select_option('pinned')
            page.get_by_test_id('chat-account').select_option('Claude Research')
            sandbox, permission = settings(page)
            sandbox.select_option('danger-full-access')
            permission.select_option('dontAsk')
            send(page, 'Third request on another route', 3)
            latest = bridge.instance_get(instance_id)
            assert latest['native_session_id'] == native_id
            assert latest['account_ref'] == 'Claude Research'
            assert latest['sandbox_mode'] == 'danger-full-access'
            assert latest['permission_mode'] == 'dontAsk'
            assert len(bridge.instances()) == 1
            calls = [json.loads(line) for line in access_playground['capture'].read_text().splitlines()]
            assert [(call['resumed'], call['sandbox'], call['approval']) for call in calls] == [
                (False, 'danger-full-access', 'never'),
                (True, 'workspace-write', 'on-request'),
                (True, 'danger-full-access', 'never'),
            ]
            assert calls[2]['previous_prompts'] == [
                'First full access request', 'Second restricted request']
            assert all(call['native_id'] == native_id for call in calls)
            page.reload(wait_until='networkidle')
            page.get_by_test_id('nav-chat').click()
            playwright_api.expect(page.get_by_test_id('chat-model')).to_have_value(CLAUDE_MODEL)
            sandbox, permission = settings(page)
            assert sandbox.input_value() == 'danger-full-access'
            assert permission.input_value() == 'dontAsk'
            page.screenshot(path=str(tmp_path / 'playground-access-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate(
                'document.documentElement.scrollWidth - document.documentElement.clientWidth') <= 1
            page.screenshot(path=str(tmp_path / 'playground-access-mobile.png'), full_page=True)
            assert errors == []
        finally:
            browser.close()


def test_failed_access_save_keeps_saved_policy_and_pending_choices(access_playground):
    bridge = access_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, access_playground['url'])
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-provider').select_option('codex')
            page.get_by_test_id('chat-model').select_option(OPENAI_MODEL)
            send(page, 'Initial restricted request', 1)
            initial = bridge.instances()[0]
            sandbox, permission = settings(page)
            sandbox.select_option('danger-full-access')
            permission.select_option('default')
            target = f'**/api/instances/{initial["id"]}'

            def unavailable(route):
                if route.request.method == 'POST':
                    route.fulfill(status=409, content_type='application/json', body=json.dumps({
                        'error': {'code': 'version_conflict', 'message': 'Fixture settings conflict.'}}))
                else:
                    route.continue_()

            page.route(target, unavailable)
            page.get_by_test_id('save-access-settings').click()
            page.locator('#toast-region').get_by_text('Fixture settings conflict.').wait_for()
            playwright_api.expect(page.get_by_test_id('save-access-settings')).to_be_enabled()
            assert sandbox.input_value() == 'danger-full-access'
            assert permission.input_value() == 'default'
            unchanged = bridge.instance_get(initial['id'])
            assert unchanged['sandbox_mode'] == 'read-only'
            assert unchanged['permission_mode'] == 'dontAsk'
            assert unchanged['native_session_id'] == initial['native_session_id']
            assert len(bridge.turns(instance_id=initial['id'])) == 1
            page.unroute(target, unavailable)
            with page.expect_response(lambda response: response.request.method == 'POST'
                                      and response.url.endswith(f'/api/instances/{initial["id"]}')) as saved:
                page.get_by_test_id('save-access-settings').click()
            assert saved.value.ok
            playwright_api.expect(page.get_by_test_id('save-access-settings')).to_be_disabled()
            assert bridge.instance_get(initial['id'])['sandbox_mode'] == 'danger-full-access'
            assert errors == []
        finally:
            browser.close()
