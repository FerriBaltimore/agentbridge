"""Exercise playground turn controls with a browser and local provider fixtures."""

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from threading import Thread
import time

import pytest

from agentbridge import Account, Bridge
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from playground.server import create_server
from test_playground_browser import NativeCapture
from test_proxy_management import EMPTY_CONFIG, local_management


playwright_api = pytest.importorskip('playwright.sync_api')
MODEL = 'fixture/turn-controls'
NATIVE_FIXTURE = Path(__file__).parent / 'fixtures' / 'test_duplex_provider.py'


def _responses():
    observed_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return {
        '/v0/management/auth-files': (200, {'files': [{
            'name': 'one.json', 'source': 'file', 'runtime_only': False,
            'provider': 'codex', 'status': 'active', 'disabled': False,
            'unavailable': False, 'auth_index': 'fixture-turn-controls',
            'account_type': 'oauth', 'id_token': {
                'chatgpt_account_id': 'fixture-turn-controls'},
            'quota': {'observed_at': observed_at, 'signals': {
                'X-Codex-Primary-Used-Percent': '12'}},
            'cooldowns': [],
        }]}, {}),
        '/v0/management/config': (200, EMPTY_CONFIG, {}),
        '/v0/management/auth-files/models?name=one.json': (
            200, {'models': [{'id': MODEL}]}, {}),
        '/v1/models?client_version=pi': (200, {'data': [{
            'slug': MODEL, 'context_window': 65536,
            'supported_reasoning_levels': [{'effort': 'low'}],
            'default_reasoning_level': 'low', 'input_modalities': ['text'],
        }]}, {}),
    }


def _fake_command(path, mode):
    bootstrap = (
        '#!/usr/bin/python3\n'
        'import json\n'
        'import os\n'
        'from pathlib import Path\n'
        'import sys\n'
        "if '--version' in sys.argv:\n"
        "    print('codex-cli 0.0.0')\n"
        '    sys.exit(0)\n'
        "capture = Path(os.environ['CODEX_HOME']) / 'fixture-calls.jsonl'\n"
        "with capture.open('a') as output:\n"
        "    output.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
    )
    if mode == 'slow':
        protocol = (Path(__file__).parent / 'fixtures/test_playground_protocol.py').read_text()
        script = '#!/usr/bin/python3\n' + protocol + '\nstart()\nimport time\ntime.sleep(30)\n'
    else:
        # The sandbox grants this executable, not a second fixture source file.
        script = bootstrap + NATIVE_FIXTURE.read_text()
    path.write_text(script)
    path.chmod(0o700)

@pytest.fixture
def local_playground(tmp_path, monkeypatch, request):
    mode = request.param
    command = tmp_path / 'fixture-codex'
    capture = NativeCapture(tmp_path / 'state')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    _fake_command(command, mode)
    monkeypatch.setenv('FIXTURE_TURNS_CLIENT_KEY', 'fixture-client-key')
    monkeypatch.setenv('FIXTURE_TURNS_MANAGEMENT_KEY', 'fixture-management-key')
    with local_management(_responses()) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            seed_authenticated_proxy_account(bridge.store, Account(
                'primary', 'codex', name='Fixture Primary', provider='codex',
                supported_models=(MODEL,),
                proxy_base_url=f'http://127.0.0.1:{port}/v1',
                key_env='FIXTURE_TURNS_CLIENT_KEY',
                management_key_env='FIXTURE_TURNS_MANAGEMENT_KEY',
                command=(str(command),)), observe_local=True)
            assert bridge.models(refresh=True)['models'][0]['id'] == MODEL
            server = create_server(tmp_path / 'state', port=0, bridge=bridge,
                                   workspace_path=workspace)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield {'url': f'http://127.0.0.1:{server.server_port}/',
                       'bridge': bridge, 'capture': capture, 'root': tmp_path}
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()


def _launch_browser(playwright):
    try:
        return playwright.chromium.launch(headless=True)
    except playwright_api.Error:
        if shutil.which('google-chrome'):
            return playwright.chromium.launch(headless=True, channel='chrome')
        pytest.skip('A Playwright Chromium browser is unavailable.')


def _page(browser, url):
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(url, wait_until='networkidle')
    return page, errors


def _wait_for_launch(capture):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if capture.exists() and capture.read_text().strip():
            return
        time.sleep(0.05)
    raise AssertionError('The fixture Codex process did not launch.')


def _pending_permission(bridge, turn_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        events = [event for event in bridge.turn_events(turn_id)
                  if event['kind'] == 'permission.required']
        if events:
            return events[0]['data']['permission_id']
        time.sleep(0.05)
    raise AssertionError(f'No permission request in {bridge.turn(turn_id)}')


@pytest.mark.parametrize('local_playground', ['slow'], indirect=True)
def test_reopen_running_turn_then_stop_without_retry_or_account_change(local_playground):
    bridge = local_playground['bridge']
    capture = local_playground['capture']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-provider').select_option('codex')
            page.get_by_test_id('chat-model').select_option(MODEL)
            page.locator('#route-settings summary').click()
            page.get_by_test_id('chat-account').select_option('Fixture Primary')
            page.get_by_test_id('chat-input').fill('Wait so I can stop this turn')
            page.get_by_test_id('chat-send').click()
            page.get_by_test_id('chat-stop').wait_for(state='visible', timeout=15000)
            assert page.get_by_test_id('chat-routing-mode').is_disabled()
            assert page.get_by_test_id('chat-account').is_disabled()
            _wait_for_launch(capture)
            assert len(bridge.runs()) == 1
            turn_id = bridge.runs()[0]['id']
            assert bridge.turn(turn_id)['account_ref'] == 'Fixture Primary'

            page.reload(wait_until='domcontentloaded')
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('conversation-item').first.click()
            page.get_by_test_id('chat-stop').wait_for(state='visible', timeout=15000)
            page.get_by_test_id('chat-stop').click()
            assert bridge.run(turn_id).wait(15)['state'] == 'cancelled'
            page.get_by_test_id('chat-stop').wait_for(state='hidden', timeout=15000)

            assert bridge.turn(turn_id)['error'] == 'user_stop'
            assert bridge.turn(turn_id)['account_ref'] == 'Fixture Primary'
            assert len(bridge.runs()) == 1
            assert len(capture.read_text().splitlines()) == 1
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize('local_playground', ['permission'], indirect=True)
@pytest.mark.parametrize('decision,expected', [('allow', 'accept'), ('deny', 'decline')])
def test_browser_permission_response_reaches_sdk(local_playground, decision, expected):
    bridge = local_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-provider').select_option('codex')
            page.get_by_test_id('chat-model').select_option(MODEL)
            page.locator('#route-settings summary').click()
            page.get_by_test_id('chat-account').select_option('Fixture Primary')
            permission = page.get_by_test_id('chat-permission')
            assert permission.locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)') == ['dontAsk', 'default']
            permission.select_option('default')
            page.get_by_test_id('chat-input').fill('Need one decision')
            page.get_by_test_id('chat-send').click()
            page.get_by_test_id('chat-stop').wait_for(state='visible', timeout=15000)
            _wait_for_launch(local_playground['capture'])
            assert len(bridge.runs()) == 1
            run = bridge.runs()[0]
            turn_id = run['id']
            instance_id = run['session_id']
            assert json.loads(run['options'])['permission_mode'] == 'default'
            permission_id = _pending_permission(bridge, turn_id)
            action = 'Allow once' if decision == 'allow' else 'Deny'
            page.get_by_test_id('chat-messages').locator(
                '[data-kind="permission.required"]').get_by_role(
                'button', name=action).wait_for(timeout=15000)
            drawer = page.locator('.event-drawer')
            drawer.locator('summary').click()
            events = page.locator('#chat-event-list')
            events.get_by_text('permission.required').wait_for(timeout=15000)
            events.get_by_role('button', name=action).click()

            assert bridge.run(turn_id).wait(15)['state'] == 'completed'
            message = bridge.messages(instance_id, role='assistant')[0]
            assert message['content'].startswith(expected + '|')
            page.get_by_test_id('nav-activity').click()
            page.locator('#activity-refresh').click()
            activity = page.get_by_test_id('activity-list')
            activity.get_by_text('permission.responded').first.wait_for(timeout=15000)
            activity.get_by_text(f'Decision: {decision}').first.wait_for()
            assert activity.get_by_role('button', name=action).count() == 0
            events = bridge.turn_events(turn_id)
            assert any(event['kind'] == 'permission.responded'
                       and event['data']['permission_id'] == permission_id
                       and event['data']['decision'] == decision for event in events)
            assert len(bridge.runs()) == 1
            assert len(local_playground['capture'].read_text().splitlines()) == 1
            assert bridge.turn(turn_id)['account_ref'] == 'Fixture Primary'
            assert errors == []
        finally:
            browser.close()
