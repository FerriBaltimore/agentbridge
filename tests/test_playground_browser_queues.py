"""Edit and deliver persistent chat queues through a real browser and SDK."""

from pathlib import Path
import json

import pytest

from agentbridge.models import TERMINAL
from agentbridge.queueing.connection import request
from test_playground_browser import _launch_browser, _page, local_playground, playwright_api


@pytest.fixture
def queued_playground(local_playground):
    fixture = (Path(__file__).parent / 'fixtures/test_queue_provider.py').read_text()
    command = local_playground['screenshot_dir'] / 'fixture-codex'
    command.write_text('#!/usr/bin/python3\nimport sys\n'
        "if '--version' in sys.argv:\n    print('codex-cli 0.0.0')\n    sys.exit(0)\n" + fixture)
    command.chmod(0o700)
    yield local_playground
    bridge = local_playground['bridge']
    for instance in bridge.instances():
        bridge.queue_pause(instance['id'])
        request(bridge.root, instance['id'], {'action': 'shutdown'}, start=False)
    for turn in bridge.runs():
        if turn['state'] not in TERMINAL:
            bridge.turn_stop(turn['id'], wait=True)


def send(page, content, delivery='queue'):
    page.get_by_test_id('chat-input').fill(content)
    if page.get_by_test_id('chat-delivery').is_visible():
        page.get_by_test_id('chat-delivery').select_option(delivery)
    page.get_by_test_id('chat-input').press('Enter')
    playwright_api.expect(page.get_by_test_id('chat-input')).to_have_value('')
    playwright_api.expect(page.get_by_test_id('chat-send')).to_be_enabled()


def open_chat(browser, playground):
    page, errors = _page(browser, playground['url'])
    page.get_by_test_id('nav-chat').click()
    page.get_by_test_id('chat-provider').select_option('codex')
    page.get_by_test_id('chat-model').select_option('fixture/openai-model')
    return page, errors


def row(page, content):
    return page.get_by_test_id('queue-item').filter(has=page.get_by_text(content, exact=True))


def test_queue_edits_survive_reload_and_resume_in_order(queued_playground):
    bridge = queued_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = open_chat(browser, queued_playground)
            send(page, 'hold:release')
            page.get_by_test_id('chat-stop').wait_for(state='visible')
            send(page, 'second')
            send(page, 'third')
            send(page, 'discard')
            instance = bridge.instances()[0]['id']
            assert bridge.queue_list(instance)['total'] == 3
            assert len(bridge.runs()) == 1
            page.set_viewport_size({'width': 320, 'height': 700})
            page.screenshot(path=str(queued_playground['screenshot_dir'] / 'chat-queue-active-320.png'))
            send_box = page.get_by_test_id('chat-send').bounding_box()
            assert send_box['y'] + send_box['height'] <= 700
            row(page, 'third').get_by_test_id('queue-up').click()
            playwright_api.expect(page.locator('.queue-content')).to_have_text(['third', 'second', 'discard'])
            row(page, 'discard').get_by_test_id('queue-remove').click()
            playwright_api.expect(page.get_by_test_id('queue-item')).to_have_count(2)
            assert page.get_by_test_id('chat-messages').get_by_text('second', exact=True).count() == 0
            page.get_by_test_id('queue-toggle').click()
            playwright_api.expect(page.locator('#chat-queue-summary')).to_contain_text('paused')
            page.reload(wait_until='domcontentloaded')
            page.get_by_test_id('nav-chat').click()
            playwright_api.expect(page.locator('.queue-content')).to_have_text(['third', 'second'])
            assert page.get_by_test_id('queue-toggle').inner_text() == 'Resume queue'
            (queued_playground['screenshot_dir'] / 'workspace/release').touch()
            page.get_by_test_id('chat-stop').wait_for(state='hidden', timeout=15000)
            assert len(bridge.runs()) == 1
            for width, height in [(1440, 900), (390, 844), (320, 700)]:
                page.set_viewport_size({'width': width, 'height': height})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                assert page.evaluate('document.documentElement.scrollHeight <= innerHeight + 1')
                assert page.locator('#chat-form').bounding_box()['y'] >= 0
                assert page.get_by_test_id('chat-send').bounding_box()['y'] < height
                page.screenshot(path=str(queued_playground['screenshot_dir'] / f'chat-queue-{width}.png'))
            page.get_by_test_id('queue-toggle').click()
            page.get_by_test_id('chat-messages').get_by_text('second|steer=', exact=True).wait_for(timeout=15000)
            playwright_api.expect(page.get_by_test_id('queue-item')).to_have_count(0)
            assert [turn['prompt'] for turn in bridge.runs()] == ['hold:release', 'third', 'second']
            assert errors == []
        finally:
            browser.close()


def test_pending_message_can_be_sent_to_active_turn(queued_playground):
    bridge = queued_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = open_chat(browser, queued_playground)
            send(page, 'hold:never')
            page.get_by_test_id('chat-stop').wait_for(state='visible')
            send(page, 'finish')
            instance = bridge.instances()[0]['id']
            message_id = bridge.queue_list(instance)['items'][0]['message_id']
            row(page, 'finish').get_by_test_id('queue-steer').click()
            page.get_by_test_id('chat-stop').wait_for(state='hidden', timeout=15000)
            page.get_by_test_id('chat-messages').get_by_text('hold:never|steer=finish', exact=True).wait_for()
            assert bridge.message_get(message_id)['state'] == 'delivered'
            assert len(bridge.runs()) == 1
            assert page.get_by_test_id('chat-messages').get_by_text('finish', exact=True).count() == 1
            assert errors == []
        finally:
            browser.close()


def test_composer_can_steer_and_interrupt_a_running_turn(queued_playground):
    bridge = queued_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = open_chat(browser, queued_playground)
            send(page, 'hold:never')
            page.get_by_test_id('chat-stop').wait_for(state='visible')
            send(page, 'tail')
            send(page, 'extra direction', 'steer')
            page.get_by_test_id('chat-messages').get_by_text('extra direction', exact=True).wait_for()
            assert len(bridge.runs()) == 1
            send(page, 'priority', 'interrupt')
            page.get_by_test_id('chat-messages').get_by_text('tail|steer=', exact=True).wait_for(timeout=15000)
            turns = bridge.runs()
            assert [turn['prompt'] for turn in turns] == ['hold:never', 'priority', 'tail']
            assert turns[0]['state'] == 'cancelled'
            assert errors == []
        finally:
            browser.close()


def test_stale_queue_edit_refreshes_without_removing_another_message(queued_playground):
    bridge = queued_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = open_chat(browser, queued_playground)
            send(page, 'hold:never')
            page.get_by_test_id('chat-stop').wait_for(state='visible')
            send(page, 'keep')
            instance = bridge.instances()[0]['id']
            def race(route):
                bridge.queue_add(instance, 'added by another client')
                route.continue_()
            page.route('**/queue/delete', race)
            row(page, 'keep').get_by_test_id('queue-remove').click()
            page.locator('#toast-region').get_by_text('version_conflict').wait_for()
            playwright_api.expect(page.locator('.queue-content')).to_have_text(['keep', 'added by another client'])
            assert len(bridge.runs()) == 1
            assert errors == []
        finally:
            browser.close()


def test_lost_steering_receipt_can_be_reconciled_after_the_turn_finishes(queued_playground):
    bridge = queued_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = open_chat(browser, queued_playground)
            send(page, 'hold:never')
            page.get_by_test_id('chat-stop').wait_for(state='visible')
            receipts = []
            def lose_receipt(route):
                if route.request.method == 'POST':
                    receipts.append(route.request.post_data_json)
                    response = route.fetch()
                    assert response.status == 200
                    route.fulfill(status=503, content_type='application/json', body=json.dumps({
                        'error': {'code': 'fixture_lost_receipt', 'message': 'Fixture receipt was lost.'}}))
                else:
                    route.continue_()
            page.route('**/messages', lose_receipt)
            page.get_by_test_id('chat-delivery').select_option('steer')
            page.get_by_test_id('chat-input').fill('finish')
            page.get_by_test_id('chat-send').click()
            page.locator('#toast-region').get_by_text('Fixture receipt was lost.').wait_for()
            page.get_by_test_id('chat-stop').wait_for(state='hidden', timeout=15000)
            playwright_api.expect(page.get_by_test_id('chat-input')).to_have_value('finish')
            assert page.get_by_test_id('chat-delivery').input_value() == 'steer'
            page.unroute('**/messages', lose_receipt)
            with page.expect_request(lambda request: request.method == 'POST'
                                     and request.url.endswith('/messages')) as retried:
                page.get_by_test_id('chat-send').click()
            playwright_api.expect(page.get_by_test_id('chat-input')).to_have_value('')
            assert retried.value.post_data_json == receipts[0]
            assert len(bridge.runs()) == 1
            assert len([item for item in bridge.messages(bridge.instances()[0]['id'])
                        if item['content'] == 'finish']) == 1
            assert errors == []
        finally:
            browser.close()
