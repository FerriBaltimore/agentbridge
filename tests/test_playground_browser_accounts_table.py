"""Exercise the account table and per-account model dialog in a browser."""

from datetime import datetime, timedelta, timezone
import json
from threading import Thread
from urllib.parse import quote

import pytest

from playground.server import create_server
from test_playground_browser import _launch_browser, local_playground, playwright_api
from test_playground_browser_layout import (
    _assert_layout_fits,
    _observed_page,
    unknown_playground,
)


def _account_row(page, name):
    return page.get_by_test_id('account-row').filter(has_text=name)


def _wait_for_account_capacity(page):
    try:
        page.wait_for_function('''() => {
      const list = document.getElementById('accounts-list');
      const shell = document.querySelector('.app-shell').getBoundingClientRect();
      const workspace = document.querySelector('.workspace').getBoundingClientRect();
      const style = getComputedStyle(list);
      const header = Number.parseFloat(style.getPropertyValue('--account-header-height')
        || style.getPropertyValue('--account-table-head-height'));
      const row = Number.parseFloat(style.getPropertyValue('--account-row-height'));
      const visible = list.querySelectorAll('[data-testid="account-row"]').length;
      return list.clientHeight > 0 && Number.isFinite(header) && row > 0
        && workspace.bottom >= shell.bottom - 1
        && visible === Math.max(1, Math.floor((list.clientHeight - header) / row));
    }''', timeout=5000)
    except playwright_api.TimeoutError as error:
        metrics = page.evaluate('''() => {
          const list = document.getElementById('accounts-list');
          const style = getComputedStyle(list);
          const header = Number.parseFloat(style.getPropertyValue('--account-header-height')
            || style.getPropertyValue('--account-table-head-height'));
          const row = Number.parseFloat(style.getPropertyValue('--account-row-height'));
          return {height:list.clientHeight, visible:list.querySelectorAll('[data-testid="account-row"]').length,
            header, row, expected:Math.max(1,Math.floor((list.clientHeight-header)/row)),
            label:document.getElementById('accounts-page-label').textContent,
            viewport:innerHeight,
            shell:document.querySelector('.app-shell').getBoundingClientRect().height,
            sidebar:document.querySelector('.sidebar').getBoundingClientRect().height,
            workspace:document.querySelector('.workspace').getBoundingClientRect().height,
            main:document.querySelector('main').getBoundingClientRect().height,
            view:document.getElementById('view-accounts').getBoundingClientRect().height,
            summary:document.getElementById('accounts-summary').getBoundingClientRect().height,
            note:document.getElementById('accounts-removal-note').getBoundingClientRect().height};
        }''')
        raise AssertionError(metrics) from error


@pytest.fixture
def many_accounts_playground(tmp_path):
    """Public SDK-shaped responses exercise layout without live providers."""

    class ManyAccountsBridge:
        def __init__(self):
            self.rows = [
                {'id': f'account-{number:02}', 'name': f'Account {number:02}',
                 'email': f'account-{number:02}@example.test', 'provider': 'codex',
                 'supported_models': []}
                for number in range(1, 33)
            ]

        def capabilities(self):
            return {'providers': ['codex']}

        def accounts(self):
            return self.rows

        def account_reference(self, account_id):
            return next(row['name'] for row in self.rows if row['id'] == account_id)

        def account_status(self, *, account_ref, refresh):
            account = next(row for row in self.rows if row['name'] == account_ref)
            return {'account_id': account['id'], 'status': 'active',
                    'authentication': {'status': 'active'},
                    'configured': {'provider': account['provider']}}

        def account_usage(self, *, account_ref, refresh=False):
            return {'supported': False, 'stale': True, 'quota_windows': [],
                    'reason': 'upstream_quota_unavailable'}

        def models(self, *, refresh=False):
            return {'items': []}

        def instances(self, *, limit=100):
            return []

    server = create_server(port=0, bridge=ManyAccountsBridge(), workspace_path=tmp_path)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()


def test_accounts_table_usage_models_and_row_actions(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-accounts').click()
            table = page.get_by_test_id('accounts-list').get_by_role('table')
            table.wait_for(state='visible')
            assert table.locator('tbody tr').count() == 2

            openai = _account_row(page, 'OpenAI Personal')
            claude = _account_row(page, 'Claude Research')
            openai.get_by_text('58% used').wait_for()
            claude.get_by_text('Unknown usage').wait_for()
            assert openai.get_by_text('0% used').count() == 0
            assert claude.get_by_text('0% used').count() == 0

            dialog = page.get_by_test_id('account-models-dialog')
            assert dialog.is_hidden()
            openai.get_by_test_id('account-models-open').click()
            dialog.get_by_role('heading', name='Models for OpenAI Personal').wait_for()
            dialog.get_by_text('fixture/openai-model', exact=True).wait_for()
            dialog.get_by_text('Effort: low, high').wait_for()
            dialog.get_by_text('Context: 131072').wait_for()
            assert dialog.get_by_text('fixture/claude-model').count() == 0
            page.keyboard.press('Escape')
            dialog.wait_for(state='hidden')

            claude.get_by_test_id('account-models-open').click()
            dialog.get_by_role('heading', name='Models for Claude Research').wait_for()
            dialog.get_by_text('fixture/claude-model', exact=True).wait_for()
            dialog.get_by_text('Usage unknown').wait_for()
            assert dialog.get_by_text('fixture/openai-model').count() == 0
            page.keyboard.press('Escape')
            dialog.wait_for(state='hidden')

            page.get_by_test_id('add-account').click()
            page.get_by_test_id('login-dialog').wait_for(state='visible')
            page.keyboard.press('Escape')
            openai.get_by_test_id('remove-account').click()
            page.get_by_test_id('remove-dialog').wait_for(state='visible')
            page.keyboard.press('Escape')
            openai.get_by_text('OpenAI Personal').wait_for()
            assert errors == []
        finally:
            browser.close()


def test_accounts_table_and_models_dialog_fit_narrow_screens(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-accounts').click()
            dialog = page.get_by_test_id('account-models-dialog')
            for width, height in [(390, 844), (320, 700)]:
                page.set_viewport_size({'width': width, 'height': height})
                page.get_by_test_id('accounts-list').get_by_role('table').wait_for()
                _assert_layout_fits(page)
                _account_row(page, 'OpenAI Personal').get_by_test_id(
                    'account-models-open').click()
                dialog.wait_for(state='visible')
                _assert_layout_fits(page)
                page.keyboard.press('Escape')
                dialog.wait_for(state='hidden')
            assert errors == []
        finally:
            browser.close()


def test_accounts_table_keeps_missing_usage_unknown(unknown_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, unknown_playground)
            page.get_by_test_id('nav-accounts').click()
            row = _account_row(page, 'Unknown Signals')
            row.get_by_text('Unknown usage').wait_for()
            assert row.get_by_text('0% used').count() == 0
            row.get_by_test_id('account-models-open').click()
            dialog = page.get_by_test_id('account-models-dialog')
            dialog.get_by_text('fixture/unknown-metadata', exact=True).wait_for()
            dialog.get_by_text('Usage unknown').wait_for()
            assert dialog.get_by_text('Effort:', exact=False).count() == 0
            assert dialog.get_by_text('Context:', exact=False).count() == 0
            assert errors == []
        finally:
            browser.close()


def test_accounts_table_marks_old_quota_as_stale(local_playground):
    entry = local_playground['openai']['/v0/management/auth-files'][1]['files'][0]
    entry['quota']['observed_at'] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat().replace('+00:00', 'Z')
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-accounts').click()
            row = _account_row(page, 'OpenAI Personal')
            row.get_by_test_id('account-models-open').wait_for()
            assert 'stale' in row.inner_text().lower()
            assert row.get_by_text('0% used').count() == 0
            assert errors == []
        finally:
            browser.close()


def test_same_named_providers_keep_independent_account_actions(local_playground):
    bridge = local_playground['bridge']
    with bridge.store.connect() as db:
        saved = db.execute('SELECT config FROM accounts WHERE id=?',
                           ('claude-research',)).fetchone()
        configuration = json.loads(saved['config'])
        configuration['name'] = 'OpenAI Personal'
        db.execute('UPDATE accounts SET config=? WHERE id=?',
                   (json.dumps(configuration), 'claude-research'))

    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            response = page.request.get(f"{local_playground['url']}api/accounts")
            accounts = response.json()['result']
            assert len(accounts) == 2
            assert {item['account_ref'] for item in accounts} == {
                'id:openai-personal', 'id:claude-research'}
            assert {item['name'] for item in accounts} == {'OpenAI Personal'}
            for account in accounts:
                reference = quote(account['account_ref'], safe='')
                status = page.request.get(
                    f"{local_playground['url']}api/accounts/{reference}/status").json()
                assert status['result']['configured']['provider'] == account['provider']

            page.get_by_test_id('nav-accounts').click()
            rows = page.get_by_test_id('account-row')
            playwright_api.expect(rows).to_have_count(2)
            codex = rows.filter(has_text='codex')
            claude = rows.filter(has_text='claude')
            codex.get_by_text('58% used').wait_for()
            claude.get_by_text('Unknown usage').wait_for()

            dialog = page.get_by_test_id('account-models-dialog')
            codex.get_by_test_id('account-models-open').click()
            dialog.get_by_text('fixture/openai-model', exact=True).wait_for()
            assert dialog.get_by_text('fixture/claude-model').count() == 0
            page.keyboard.press('Escape')
            claude.get_by_test_id('account-models-open').click()
            dialog.get_by_text('fixture/claude-model', exact=True).wait_for()
            assert dialog.get_by_text('fixture/openai-model').count() == 0
            page.keyboard.press('Escape')

            claude.get_by_test_id('remove-account').click()
            page.get_by_test_id('remove-confirm').click()
            playwright_api.expect(rows).to_have_count(1)
            assert codex.is_visible()
            assert [account.provider for account in bridge.accounts()] == ['codex']
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize('width,height', [(1440, 900), (390, 844), (320, 700)])
def test_many_accounts_pages_fit_viewport_and_visit_every_row(
        many_accounts_playground, width, height):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page = browser.new_page(viewport={'width': width, 'height': height})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(many_accounts_playground, wait_until='networkidle')
            page.get_by_test_id('nav-accounts').click()
            _wait_for_account_capacity(page)
            rows = page.get_by_test_id('account-row')
            label = page.locator('#accounts-page-label')
            previous = page.locator('#accounts-prev')
            following = page.locator('#accounts-next')
            first_page = rows.locator('.account-name').all_text_contents()
            assert 1 <= len(first_page) < 32
            assert previous.is_disabled() and following.is_enabled()
            assert label.inner_text() == f'1–{len(first_page)} of 32'

            following.click()
            next_page = rows.locator('.account-name').all_text_contents()
            assert next_page[0] == f'Account {len(first_page) + 1:02}'
            assert previous.is_enabled()
            previous.click()
            assert rows.locator('.account-name').all_text_contents() == first_page

            seen = []
            while True:
                names = rows.locator('.account-name').all_text_contents()
                assert names and not set(names) & set(seen)
                expected_start = len(seen) + 1
                assert label.inner_text() == (
                    f'{expected_start}–{len(seen) + len(names)} of 32')
                geometry = page.evaluate('''() => {
                  const list = document.getElementById('accounts-list');
                  const view = document.getElementById('view-accounts');
                  const pager = document.getElementById('accounts-pager');
                  const table = list.querySelector('table');
                  return {
                    documentOverflow: document.documentElement.scrollHeight
                      - document.documentElement.clientHeight,
                    listOverflow: list.scrollHeight - list.clientHeight,
                    tableBottom: table.getBoundingClientRect().bottom,
                    listBottom: list.getBoundingClientRect().bottom,
                    pagerBottom: pager.getBoundingClientRect().bottom,
                    viewBottom: view.getBoundingClientRect().bottom,
                    shellBottom: document.querySelector('.app-shell').getBoundingClientRect().bottom,
                    workspaceBottom: document.querySelector('.workspace').getBoundingClientRect().bottom,
                    lastRowBottom: table.tBodies[0].lastElementChild
                      .getBoundingClientRect().bottom,
                  };
                }''')
                assert geometry['documentOverflow'] <= 1, geometry
                assert geometry['listOverflow'] <= 1, geometry
                assert geometry['tableBottom'] <= geometry['listBottom'] + 1, geometry
                assert geometry['lastRowBottom'] <= geometry['listBottom'] + 1, geometry
                assert geometry['pagerBottom'] <= geometry['viewBottom'] + 1, geometry
                assert geometry['workspaceBottom'] >= geometry['shellBottom'] - 1, geometry
                _assert_layout_fits(page)
                seen.extend(names)
                if following.is_disabled():
                    break
                following.click()
            assert seen == [f'Account {number:02}' for number in range(1, 33)]
            assert errors == []
        finally:
            browser.close()


def test_account_page_size_reflows_with_viewport_height(many_accounts_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.goto(many_accounts_playground, wait_until='networkidle')
            page.get_by_test_id('nav-accounts').click()
            counts = []
            for width, height in [(1440, 900), (390, 844), (320, 700)]:
                page.set_viewport_size({'width': width, 'height': height})
                _wait_for_account_capacity(page)
                counts.append(page.get_by_test_id('account-row').count())
            assert counts[0] > counts[1] > counts[2], counts
        finally:
            browser.close()
