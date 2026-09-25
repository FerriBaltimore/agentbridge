"""Browser checks for the SDK-backed live timeline and last viewed chat."""

from pathlib import Path
import textwrap

from test_playground_browser import _launch_browser, _page, local_playground, playwright_api


def _route(page):
    page.get_by_test_id('chat-provider').select_option('codex')
    page.get_by_test_id('chat-model').select_option('fixture/openai-model')


def _send(page, prompt):
    _route(page)
    page.get_by_test_id('chat-input').fill(prompt)
    page.get_by_test_id('chat-send').click()
    page.get_by_test_id('chat-messages').get_by_text('Browser fixture answer').wait_for(timeout=15000)
    playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(timeout=15000)


def _streaming_command(path: Path):
    protocol = (Path(__file__).parent / 'fixtures/test_playground_protocol.py').read_text()
    path.write_text('#!/usr/bin/python3\n' + protocol + textwrap.dedent('''\

        import json
        import sys
        import time

        start()
        emit({'type': 'thread.started', 'thread_id': 'fixture-stream'})
        emit({'type': 'turn.started'})
        emit({'type': 'item.started', 'item': {'id': 'fixture-tool',
            'type': 'command_execution', 'command': 'SECRET_TOOL_INPUT', 'status': 'running'}})
        time.sleep(1.1)
        emit({'type': 'item.started', 'item': {'id': 'fixture-compaction',
            'type': 'context_compaction'}})
        time.sleep(0.7)
        emit({'type': 'item.completed', 'item': {'id': 'fixture-compaction',
            'type': 'context_compaction'}})
        time.sleep(0.7)
        emit({'type': 'bridge_text_delta', 'text': 'Partial fixture answer'})
        time.sleep(1.1)
        emit({'type': 'item.completed', 'item': {'id': 'fixture-tool',
            'type': 'command_execution', 'status': 'completed',
            'aggregated_output': 'SECRET_TOOL_OUTPUT'}})
        emit({'type': 'item.completed', 'item': {'id': 'private-reasoning',
            'type': 'reasoning', 'text': 'PRIVATE_REASONING_TEXT'}})
        emit({'type': 'item.completed', 'item': {'id': 'fixture-answer',
            'type': 'agent_message', 'text': 'Final fixture answer'}})
        emit({'type': 'turn.completed'})
    '''))
    path.chmod(0o700)


def test_chat_restores_last_viewed_conversation_and_falls_back_to_latest(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-chat').click()
            _send(page, 'First remembered conversation')
            page.locator('#new-conversation').click()
            _send(page, 'Second remembered conversation')
            conversations = page.get_by_test_id('conversation-item')
            playwright_api.expect(conversations).to_have_count(2)
            conversations.nth(1).click()
            page.get_by_test_id('chat-messages').get_by_text('First remembered conversation').wait_for()
            first_id = page.evaluate("localStorage.getItem('agentbridge.playground.last_instance_id')")
            assert first_id

            page.reload(wait_until='networkidle')
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-messages').get_by_text('First remembered conversation').wait_for()
            assert page.get_by_test_id('conversation-item').nth(1).get_attribute('aria-current') == 'true'

            page.evaluate("localStorage.setItem('agentbridge.playground.last_instance_id', 'missing-instance')")
            page.reload(wait_until='networkidle')
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-messages').get_by_text('Second remembered conversation').wait_for()
            assert page.get_by_test_id('conversation-item').first.get_attribute('aria-current') == 'true'
            assert errors == []
        finally:
            browser.close()


def test_chat_streams_tool_compaction_and_answer_in_sequence(local_playground):
    _streaming_command(local_playground['screenshot_dir'] / 'fixture-codex')
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-chat').click()
            page.set_viewport_size({'width': 320, 'height': 700})
            _route(page)
            page.evaluate('''() => {
              const NativeEventSource = window.EventSource;
              window.streamTrace = [];
              window.EventSource = class extends NativeEventSource {
                constructor(url, options) {
                  super(url, options);
                  const trace = { url: String(url), source: this, kinds: [], ended: false };
                  this.addEventListener('message', (message) => {
                    try { trace.kinds.push(JSON.parse(message.data).kind); } catch { /* Fixture check. */ }
                  });
                  this.addEventListener('end', () => { trace.ended = true; });
                  window.streamTrace.push(trace);
                }
              };
            }''')
            page.get_by_test_id('chat-input').fill('Show the live timeline')
            with page.expect_response(lambda response: '/api/turns/' in response.url
                                      and '/stream' in response.url) as streamed:
                page.get_by_test_id('chat-send').click()
            assert streamed.value.status == 200
            assert streamed.value.headers['content-type'].startswith('text/event-stream')
            page.get_by_test_id('chat-stop').wait_for(state='visible', timeout=15000)
            timeline = page.get_by_test_id('chat-messages')
            timeline.locator('[data-kind="tool.started"]').get_by_text('running').wait_for(timeout=15000)
            timeline.locator('[data-kind="context.compacting"]').wait_for(timeout=15000)
            timeline.locator('[data-kind="context.compacted"]').wait_for(timeout=15000)
            overflow = timeline.evaluate('(item) => item.scrollHeight - item.clientHeight')
            assert overflow > 90
            timeline.evaluate('(item) => { item.scrollTop = 0; }')
            timeline.get_by_text('Partial fixture answer').wait_for(timeout=15000)
            page.wait_for_function('''() => window.streamTrace.some((trace) =>
              ['tool.started', 'context.compacting', 'context.compacted', 'message.delta']
                .every((kind) => trace.kinds.includes(kind)))''', timeout=15000)
            assert timeline.evaluate('(item) => item.scrollTop') < 5
            assert page.get_by_test_id('chat-stop').is_visible()
            timeline.locator('[data-kind="tool.completed"]').wait_for(timeout=15000)
            timeline.get_by_text('Final fixture answer').wait_for(timeout=15000)
            page.get_by_test_id('chat-stop').wait_for(state='hidden', timeout=15000)
            page.wait_for_function('''() => window.streamTrace.length > 0
              && window.streamTrace.every((trace) => trace.source.readyState === 2)''',
              timeout=15000)
            window_stream = page.evaluate('''() => window.streamTrace.map((trace) => ({
              url: trace.url, kinds: trace.kinds, ended: trace.ended,
              readyState: trace.source.readyState,
            }))''')
            assert window_stream
            assert '/stream' in window_stream[0]['url']
            kinds = timeline.locator('[data-testid="chat-timeline-event"]').evaluate_all(
                '(items) => items.map((item) => item.dataset.kind)')
            assert kinds.index('tool.started') < kinds.index('context.compacted')
            assert kinds.index('context.compacting') < kinds.index('context.compacted')
            assert kinds.index('context.compacted') < kinds.index('tool.completed')
            assert 'SECRET_TOOL_INPUT' not in timeline.inner_text()
            assert 'SECRET_TOOL_OUTPUT' not in timeline.inner_text()
            assert 'PRIVATE_REASONING_TEXT' not in timeline.inner_text()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert errors == []
        finally:
            browser.close()
