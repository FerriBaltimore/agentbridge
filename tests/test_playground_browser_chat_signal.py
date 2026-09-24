"""Show conversation content and meaningful signals without routine event noise."""

from test_playground_browser import _launch_browser, _page, local_playground, playwright_api


def _provider_events(path, *, gap='routine', failure=False):
    events = [{'type': 'thread.started', 'thread_id': 'fixture-chat-signal'}]
    if gap == 'routine':
        events.append({'type': 'bridge_gap'})
        events.append({'type': 'item.completed', 'item': {
            'id': 'fixture-native-error-item', 'type': 'error'}})
    elif gap == 'substantive':
        events.append({'type': 'item.completed', 'item': {
            'id': 'fixture-unknown-item', 'type': 'fixture_future_item'}})
    if failure:
        events.append({'type': 'turn.failed', 'error': {'code': 'provider_unavailable'}})
    else:
        events.extend([
            {'type': 'item.completed', 'item': {
                'id': 'fixture-answer', 'type': 'agent_message', 'text': 'Hello from fixture'}},
            {'type': 'turn.completed'},
        ])
    path.write_text(
        '#!/usr/bin/python3\n'
        'import json\n'
        'import sys\n'
        'if "--version" in sys.argv:\n'
        '    print("codex-cli 0.0.0")\n'
        '    sys.exit(0)\n'
        'sys.stdin.read()\n'
        f'for event in {events!r}:\n'
        '    print(json.dumps(event), flush=True)\n'
    )
    path.chmod(0o700)


def _send(page, prompt='hi'):
    page.get_by_test_id('nav-chat').click()
    page.get_by_test_id('chat-provider').select_option('codex')
    page.get_by_test_id('chat-model').select_option('fixture/openai-model')
    page.get_by_test_id('chat-input').fill(prompt)
    page.get_by_test_id('chat-send').click()


def _event_kinds(page):
    return page.get_by_test_id('chat-timeline-event').evaluate_all(
        '(items) => items.map((item) => item.dataset.kind)')


def test_simple_chat_shows_only_user_and_answer_with_routine_gap_in_activity(local_playground):
    _provider_events(local_playground['screenshot_dir'] / 'fixture-codex')
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            _send(page)
            chat = page.get_by_test_id('chat-messages')
            chat.get_by_text('Hello from fixture').wait_for(timeout=15000)
            playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(timeout=15000)
            messages = chat.get_by_test_id('chat-message')
            playwright_api.expect(messages).to_have_count(2)
            assert messages.evaluate_all('(items) => items.map((item) => item.dataset.role)') == [
                'user', 'assistant']
            assert messages.nth(0).get_by_text('hi').is_visible()
            assert messages.nth(1).get_by_text('Hello from fixture').is_visible()
            assert _event_kinds(page) == []
            turn_id = local_playground['bridge'].runs()[0]['id']
            gaps = [(event['data'].get('reason'), event['data'].get('native_type'))
                    for event in local_playground['bridge'].turn_events(turn_id)
                    if event['kind'] == 'recovery.gap']
            assert gaps == [
                ('unsupported_provider_observation', None),
                ('unsupported_item', 'error'),
            ]
            page.get_by_test_id('nav-activity').click()
            activity = page.get_by_test_id('activity-list')
            for kind in ('route.selected', 'run.started', 'recovery.gap', 'run.finished'):
                activity.get_by_text(kind, exact=True).first.wait_for()
            assert errors == []
        finally:
            browser.close()


def test_substantive_observation_gap_remains_in_completed_chat(local_playground):
    _provider_events(local_playground['screenshot_dir'] / 'fixture-codex', gap='substantive')
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            _send(page, 'inspect event gap')
            page.get_by_test_id('chat-messages').get_by_text('Hello from fixture').wait_for(
                timeout=15000)
            playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(timeout=15000)
            assert _event_kinds(page) == ['recovery.gap']
            assert errors == []
        finally:
            browser.close()


def test_failed_chat_keeps_actionable_error_without_routine_gap(local_playground):
    _provider_events(local_playground['screenshot_dir'] / 'fixture-codex', failure=True)
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            _send(page, 'trigger fixture failure')
            page.get_by_test_id('chat-messages').locator('[data-kind="run.error"]').first.wait_for(
                timeout=15000)
            playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(timeout=15000)
            kinds = _event_kinds(page)
            assert 'run.error' in kinds
            assert 'recovery.gap' not in kinds
            assert errors == []
        finally:
            browser.close()
