"""Exercise the local playground with a real browser and simulated providers."""

from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from threading import Thread
import textwrap

import pytest

from agentbridge import Account, Bridge
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from playground.server import create_server
from test_proxy_management import EMPTY_CONFIG, local_management


playwright_api = pytest.importorskip('playwright.sync_api')


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _proxy_responses(provider, model, *, used=None, credential=True):
    entry = {
        'name': 'one.json', 'source': 'file', 'runtime_only': False,
        'provider': provider, 'status': 'active', 'disabled': False,
        'unavailable': False, 'auth_index': f'fixture-{provider}',
        'account_type': 'oauth', 'email': f'{provider}@example.test',
        'cooldowns': [],
    }
    if provider == 'codex':
        entry['id_token'] = {'chatgpt_account_id': 'fixture-personal'}
    if used is not None:
        entry['quota'] = {'observed_at': _now(),
                          'signals': {'X-Codex-Primary-Used-Percent': str(used)}}
    return {
        '/v0/management/auth-files': (200, {'files': [entry] if credential else []}, {}),
        '/v0/management/config': (200, EMPTY_CONFIG, {}),
        '/v0/management/auth-files/models?name=one.json': (
            200, {'models': [{'id': model}]}, {}),
        '/v1/models?client_version=pi': (200, {'data': [{
            'slug': model, 'context_window': 131072,
            'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'high'}],
            'default_reasoning_level': 'high', 'input_modalities': ['text', 'image'],
            'private_token': 'never-render-this',
        }]}, {}),
    }


class NativeCapture:
    """Read simulated native calls from their isolated Codex session homes."""

    def __init__(self, state_root):
        self.root = state_root / 'codex-runtime'

    def _files(self):
        return sorted(self.root.glob('*/fixture-calls.jsonl'),
                      key=lambda path: path.stat().st_mtime_ns)

    def exists(self):
        return bool(self._files())

    def read_text(self):
        return ''.join(path.read_text() for path in self._files())


def _fake_codex(path):
    path.write_text(textwrap.dedent('''\
        #!/usr/bin/python3
        import json
        import os
        from pathlib import Path
        import sys

        if '--version' in sys.argv:
            print('codex-cli 0.0.0')
            sys.exit(0)
        prompt = sys.stdin.read()
        capture = Path(os.environ['CODEX_HOME']) / 'fixture-calls.jsonl'
        with capture.open('a') as stream:
            stream.write(json.dumps({'argv': sys.argv[1:], 'prompt': prompt}) + '\\n')
        print(json.dumps({'type': 'thread.started', 'thread_id': 'fixture-native-session'}), flush=True)
        print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
            'text': 'Browser fixture answer'}}), flush=True)
        print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 12,
            'output_tokens': 4}}), flush=True)
    '''))
    path.chmod(0o700)


class FakeGrantBridge:
    def __init__(self, responses):
        self.responses = responses

    def configuration(self):
        return {'adapter': 'fixture', 'data_dir': None, 'node': 'fixture'}

    def proxy_start(self, provider, base_url, management_key_env):
        assert provider == 'grok'
        assert base_url.endswith('/v1')
        assert management_key_env == 'LAB_GROK_MANAGEMENT_KEY'
        status, body, headers = self.responses['/v0/management/auth-files']
        self.responses['/v0/management/auth-files'] = (status, {
            'files': _proxy_responses('grok', 'fixture/grok-model')[
                '/v0/management/auth-files'][1]['files']}, headers)
        return {'id': 'fixture-oauth-state', 'provider': provider,
                'status': 'awaiting_user',
                'authorizationUrl': 'https://auth.example.test/authorize'}

    def proxy_status(self, state, provider, base_url, management_key_env):
        assert state == 'fixture-oauth-state' and provider == 'grok'
        return {'id': state, 'provider': provider, 'status': 'authorized'}

    def proxy_cancel(self, state, provider, base_url, management_key_env):
        return {'id': state, 'provider': provider, 'status': 'cancelled'}

    def close(self):
        pass


class FixtureManagedProxy:
    """Point managed login at a local fixture without bypassing the SDK branch."""

    def __init__(self, monkeypatch, port, *, prefix='GROK'):
        self.monkeypatch = monkeypatch
        self.route = {
            'proxy_base_url': f'http://127.0.0.1:{port}/v1',
            'key_env': f'LAB_{prefix}_CLIENT_KEY',
            'management_key_env': f'LAB_{prefix}_MANAGEMENT_KEY',
        }
        self.account_ids = set()
        self.provisioned = []
        self.ensured = []
        self.retired = []

    def _install_keys(self):
        self.monkeypatch.setenv(self.route['key_env'], 'fixture-client')
        self.monkeypatch.setenv(self.route['management_key_env'], 'fixture-management')

    def provision(self, account_id):
        assert account_id not in self.account_ids
        self.account_ids.add(account_id)
        self.provisioned.append(account_id)
        self._install_keys()
        return dict(self.route)

    def ensure(self, account_id, base_url):
        assert account_id in self.account_ids
        assert base_url == self.route['proxy_base_url']
        self.ensured.append(account_id)
        self._install_keys()
        return dict(self.route)

    def is_managed(self, route_config, account_id=None):
        return (isinstance(route_config, dict)
                and (account_id is None or account_id in self.account_ids)
                and all(route_config.get(key) == value for key, value in self.route.items()))

    def retire(self, account_id):
        assert account_id in self.account_ids
        self.retired.append(account_id)
        return {'retired': True, 'upstream_credential_removed': False}


def configure_fixture_login(bridge, monkeypatch, port):
    managed = FixtureManagedProxy(monkeypatch, port)
    bridge.managed_proxy = managed
    bridge.authentication.managed_proxy = managed
    bridge.routes.managed_proxy = managed
    return managed


@pytest.fixture
def local_playground(tmp_path, monkeypatch):
    fake_codex = tmp_path / 'fixture-codex'
    capture = NativeCapture(tmp_path / 'state')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    _fake_codex(fake_codex)
    monkeypatch.setenv('LAB_OPENAI_CLIENT_KEY', 'fixture-openai-client')
    monkeypatch.setenv('LAB_OPENAI_MANAGEMENT_KEY', 'fixture-openai-management')
    monkeypatch.setenv('LAB_CLAUDE_CLIENT_KEY', 'fixture-claude-client')
    monkeypatch.setenv('LAB_CLAUDE_MANAGEMENT_KEY', 'fixture-claude-management')
    monkeypatch.setenv('LAB_GROK_CLIENT_KEY', 'fixture-grok-client')
    monkeypatch.setenv('LAB_GROK_MANAGEMENT_KEY', 'fixture-grok-management')
    openai = _proxy_responses('codex', 'fixture/openai-model', used=58)
    claude = _proxy_responses('claude', 'fixture/claude-model')
    grok = _proxy_responses('grok', 'fixture/grok-model', credential=False)
    with ExitStack() as stack:
        openai_port, openai_seen = stack.enter_context(local_management(openai))
        claude_port, _ = stack.enter_context(local_management(claude))
        grok_port, _ = stack.enter_context(local_management(grok))
        bridge = stack.enter_context(Bridge(tmp_path / 'state'))
        for account_id, name, provider, port, model, prefix in (
            ('openai-personal', 'OpenAI Personal', 'codex', openai_port,
             'fixture/openai-model', 'OPENAI'),
            ('claude-research', 'Claude Research', 'claude', claude_port,
             'fixture/claude-model', 'CLAUDE'),
        ):
            account = Account(account_id, 'codex', name=name, provider=provider,
                              supported_models=(model,),
                              proxy_base_url=f'http://127.0.0.1:{port}/v1',
                              key_env=f'LAB_{prefix}_CLIENT_KEY',
                              management_key_env=f'LAB_{prefix}_MANAGEMENT_KEY',
                              command=(str(fake_codex),))
            seed_authenticated_proxy_account(bridge.store, account, observe_local=True)
        assert len(bridge.models(refresh=True)['models']) == 2
        fake_grantbridge = FakeGrantBridge(grok)
        monkeypatch.setattr('agentbridge.authentication.GrantBridgeClient',
                            lambda *args, **kwargs: fake_grantbridge)
        managed = configure_fixture_login(bridge, monkeypatch, grok_port)
        server = create_server(tmp_path / 'state', port=0, bridge=bridge,
                               workspace_path=workspace)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield {
                'url': f'http://127.0.0.1:{server.server_port}/',
                'bridge': bridge, 'capture': capture, 'openai': openai,
                'claude': claude,
                'openai_seen': openai_seen, 'grok_port': grok_port,
                'managed': managed,
                'screenshot_dir': tmp_path,
            }
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def _page(browser, url):
    page = browser.new_page(viewport={'width': 1440, 'height': 1000},
                            device_scale_factor=1)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(url, wait_until='networkidle')
    return page, errors


def _launch_browser(playwright):
    try:
        return playwright.chromium.launch(headless=True)
    except playwright_api.Error:
        if shutil.which('google-chrome'):
            return playwright.chromium.launch(headless=True, channel='chrome')
        pytest.skip('A Playwright Chromium browser is unavailable.')


def test_browser_shows_sdk_catalog_usage_and_runs_chat(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-overview').click()
            capacity = page.get_by_test_id('overview-capacity-list')
            capacity.get_by_text('OpenAI Personal').wait_for()
            capacity.get_by_text('58% used').wait_for()
            page.locator('#metric-ready').get_by_text('2').wait_for()
            page.locator('#metric-fresh').get_by_text('1').wait_for()
            shots = local_playground['screenshot_dir']
            page.screenshot(path=str(shots / 'playground-overview-desktop.png'),
                            full_page=True)
            page.get_by_test_id('nav-accounts').click()
            accounts = page.get_by_test_id('accounts-list')
            openai_row = accounts.get_by_test_id('account-row').filter(has_text='OpenAI Personal')
            openai_row.get_by_text('OpenAI Personal').wait_for()
            accounts.get_by_text('Claude Research').wait_for()
            accounts.get_by_text('Unknown usage').wait_for()
            openai_row.get_by_text('active', exact=True).wait_for()
            page.locator('#accounts-summary').get_by_text(
                '2 with an active or usable observation').wait_for()
            openai_row.get_by_test_id('account-usage-open').click()
            usage_dialog = page.get_by_test_id('account-usage-dialog')
            usage_dialog.get_by_text('42% remaining', exact=False).wait_for()
            page.keyboard.press('Escape')
            usage_dialog.wait_for(state='hidden')
            openai_row.get_by_test_id('account-models-open').click()
            models_dialog = page.get_by_test_id('account-models-dialog')
            models_dialog.get_by_text('Health: Active, binding verified').wait_for()
            models_dialog.get_by_text('Effort: low, high').wait_for()
            models_dialog.get_by_text('Context: 131072').wait_for()
            page.screenshot(path=str(shots / 'playground-accounts-desktop.png'),
                            full_page=True)

            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(shots / 'playground-accounts-mobile.png'),
                            full_page=True)
            overflow = page.evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth')
            assert overflow <= 1
            page.keyboard.press('Escape')
            models_dialog.wait_for(state='hidden')
            page.set_viewport_size({'width': 1440, 'height': 1000})

            page.get_by_test_id('nav-chat').click()
            provider = page.get_by_test_id('chat-provider')
            assert {'codex', 'claude'} <= set(provider.locator('option').evaluate_all(
                '(options) => options.map((option) => option.value)'))
            provider.select_option('claude')
            assert 'fixture/claude-model' in page.get_by_test_id('chat-model').locator(
                'option').all_text_contents()
            assert 'fixture/openai-model' not in page.get_by_test_id('chat-model').locator(
                'option').all_text_contents()
            provider.select_option('codex')
            model = page.get_by_test_id('chat-model')
            model.select_option('fixture/openai-model')
            assert 'fixture/claude-model' not in model.locator('option').all_text_contents()
            effort = page.locator('#chat-effort')
            effort.wait_for(state='visible')
            assert set(effort.locator('option').all_text_contents()) >= {'low', 'high'}
            assert 'private-level' not in effort.locator('option').all_text_contents()
            effort.select_option('high')
            context = page.locator('#chat-context')
            context.wait_for(state='visible')
            assert context.evaluate('(element) => element.tagName') == 'SELECT'
            assert context.locator('option').evaluate_all(
                '(items) => items.map((item) => item.value)') == ['', '131072']
            context.select_option('131072')
            page.get_by_test_id('chat-input').fill('Hello from the browser fixture')
            page.get_by_test_id('chat-send').click()
            page.get_by_test_id('chat-messages').get_by_text('Browser fixture answer').wait_for(timeout=15000)
            calls = [json.loads(line) for line in local_playground['capture'].read_text().splitlines()]
            assert len(calls) == 1
            assert 'Hello from the browser fixture' in calls[0]['prompt']
            assert '--model' in calls[0]['argv']
            assert 'fixture/openai-model' in calls[0]['argv']
            assert 'model_context_window=131072' in calls[0]['argv']
            assert 'model_reasoning_effort="high"' in calls[0]['argv']
            page.locator('#toast-region').get_by_text('Turn completed.').wait_for(
                timeout=15000)
            page.locator('#toast-region .toast').first.wait_for(
                state='detached', timeout=10000)
            page.screenshot(path=str(shots / 'playground-chat-desktop.png'),
                            full_page=True)

            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(shots / 'playground-chat-mobile.png'),
                            full_page=True)
            overflow = page.evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth')
            assert overflow <= 1
            page.get_by_test_id('nav-activity').click()
            assert page.get_by_test_id('activity-list').locator('.event-item').count() > 0
            assert errors == []
        finally:
            browser.close()


def test_browser_login_and_local_account_removal(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-accounts').click()
            row = page.get_by_test_id('accounts-list').get_by_test_id('account-row').filter(
                has_text='OpenAI Personal')
            row.get_by_test_id('remove-account').click()
            page.locator('#remove-dialog').get_by_text(
                'Its provider authorization is not revoked.').wait_for()
            page.get_by_test_id('remove-confirm').click()
            page.get_by_test_id('accounts-list').get_by_text('OpenAI Personal').wait_for(state='detached')
            assert len(local_playground['openai']['/v0/management/auth-files'][1]['files']) == 1

            page.get_by_test_id('add-account').click()
            page.get_by_test_id('login-provider').select_option('grok')
            page.get_by_test_id('login-name').fill('Grok Lab')
            assert page.get_by_test_id('login-dialog').locator('input, select').count() == 2
            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith('/api/accounts/login/start')) as sent:
                page.get_by_test_id('login-start').click()
            assert sent.value.post_data_json == {'provider': 'grok', 'name': 'Grok Lab'}
            page.get_by_test_id('accounts-list').get_by_text('Grok Lab').wait_for(timeout=20000)
            assert len(local_playground['managed'].provisioned) == 1
            assert local_playground['managed'].ensured
            assert any(account.name == 'Grok Lab' and account.provider == 'grok'
                       for account in local_playground['bridge'].accounts())
            page.get_by_test_id('nav-chat').click()
            provider = page.get_by_test_id('chat-provider')
            values = provider.locator('option').evaluate_all(
                '(options) => options.map((option) => option.value)')
            assert 'grok' in values and 'codex' not in values
            provider.select_option('grok')
            assert 'fixture/grok-model' in page.get_by_test_id('chat-model').locator(
                'option').all_text_contents()
            page.get_by_test_id('nav-accounts').click()
            grok = page.get_by_test_id('accounts-list').get_by_test_id('account-row').filter(
                has_text='Grok Lab')
            grok.get_by_test_id('remove-account').click()
            page.get_by_test_id('remove-confirm').click()
            grok.wait_for(state='detached')
            assert local_playground['managed'].retired == local_playground['managed'].provisioned
            assert errors == []
        finally:
            browser.close()
