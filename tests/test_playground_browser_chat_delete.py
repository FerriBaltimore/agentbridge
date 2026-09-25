"""Conversation deletion in Chat follows the durable SDK result."""

import json

from test_playground_browser import _launch_browser, _page, local_playground, playwright_api


def _send(page, prompt):
    page.get_by_test_id('chat-provider').select_option('codex')
    page.get_by_test_id('chat-model').select_option('fixture/openai-model')
    page.get_by_test_id('chat-input').fill(prompt)
    page.get_by_test_id('chat-send').click()
    page.get_by_test_id('chat-messages').get_by_text('Browser fixture answer').wait_for(timeout=15000)
    playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(timeout=15000)


def test_chat_deletes_selected_and_other_conversations_only_after_confirmation(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-chat').click()
            _send(page, 'First conversation')
            page.locator('#new-conversation').click()
            _send(page, 'Second conversation')
            conversations = page.get_by_test_id('conversation-item')
            playwright_api.expect(conversations).to_have_count(2)
            selected_id = page.evaluate(
                "localStorage.getItem('agentbridge.playground.last_instance_id')")

            page.get_by_test_id('delete-conversation').last.click()
            dialog = page.get_by_test_id('delete-conversation-dialog')
            playwright_api.expect(dialog).to_be_visible()
            page.locator('#delete-conversation-cancel').click()
            playwright_api.expect(conversations).to_have_count(2)

            page.get_by_test_id('delete-conversation').last.click()
            page.get_by_test_id('delete-conversation-confirm').click()
            playwright_api.expect(conversations).to_have_count(1)
            page.get_by_test_id('chat-messages').get_by_text('Second conversation').wait_for()
            assert page.evaluate(
                "localStorage.getItem('agentbridge.playground.last_instance_id')") == selected_id

            def pending_response(route):
                if route.request.method != 'DELETE':
                    route.continue_()
                    return
                route.fulfill(status=200, content_type='application/json', body=json.dumps({
                    'result': {'instance_id': selected_id, 'deleted': False, 'pending': True}}))

            page.route('**/api/instances/*', pending_response)
            page.get_by_test_id('delete-conversation').click()
            page.get_by_test_id('delete-conversation-confirm').click()
            playwright_api.expect(page.get_by_test_id('pending-conversation-delete')).to_have_count(1)
            playwright_api.expect(conversations).to_have_count(0)
            playwright_api.expect(page.locator('#chat-conversation-title')).to_have_text(
                'New conversation')
            assert page.evaluate(
                "localStorage.getItem('agentbridge.playground.last_instance_id')") is None
            assert selected_id in page.evaluate(
                "localStorage.getItem('agentbridge.playground.pending_conversation_deletions')")
            page.unroute('**/api/instances/*', pending_response)

            page.reload(wait_until='networkidle')
            page.get_by_test_id('nav-chat').click()
            playwright_api.expect(page.get_by_test_id('pending-conversation-delete')).to_have_count(1)
            playwright_api.expect(conversations).to_have_count(0)
            playwright_api.expect(page.locator('#chat-conversation-title')).to_have_text(
                'New conversation')
            page.get_by_test_id('retry-conversation-delete').click()
            playwright_api.expect(page.get_by_test_id('pending-conversation-delete')).to_have_count(0)
            playwright_api.expect(conversations).to_have_count(0)
            playwright_api.expect(page.locator('#chat-conversation-title')).to_have_text(
                'New conversation')
            playwright_api.expect(page.get_by_test_id('chat-messages').get_by_text(
                'What can we work on?')).to_be_visible()
            assert page.evaluate(
                "localStorage.getItem('agentbridge.playground.last_instance_id')") is None
            assert page.evaluate(
                "localStorage.getItem('agentbridge.playground.pending_conversation_deletions')") is None
            page.reload(wait_until='networkidle')
            page.get_by_test_id('nav-chat').click()
            playwright_api.expect(conversations).to_have_count(0)
            assert errors == []
        finally:
            browser.close()


def test_chat_retries_cleanup_after_error_and_reload(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            page.get_by_test_id('nav-chat').click()
            _send(page, 'Cleanup retry conversation')
            instance_id = page.evaluate(
                "localStorage.getItem('agentbridge.playground.last_instance_id')")

            def cleanup_error(route):
                if route.request.method != 'DELETE':
                    route.continue_()
                    return
                route.fulfill(status=400, content_type='application/json', body=json.dumps({
                    'error': {'code': 'instance_cleanup_failed',
                              'message': 'Local cleanup needs another attempt.'}}))

            page.route('**/api/instances/*', cleanup_error)
            page.get_by_test_id('delete-conversation').click()
            page.get_by_test_id('delete-conversation-confirm').click()
            playwright_api.expect(page.get_by_test_id('pending-conversation-delete')).to_have_count(1)
            playwright_api.expect(page.get_by_test_id('conversation-item')).to_have_count(0)
            page.unroute('**/api/instances/*', cleanup_error)

            page.reload(wait_until='networkidle')
            page.get_by_test_id('nav-chat').click()
            playwright_api.expect(page.get_by_test_id('pending-conversation-delete')).to_have_count(1)
            playwright_api.expect(page.get_by_test_id('pending-conversation-delete').get_by_text(
                'fixture/openai-model')).to_be_visible()
            assert page.evaluate(
                "localStorage.getItem('agentbridge.playground.last_instance_id')") is None
            assert instance_id in page.evaluate(
                "localStorage.getItem('agentbridge.playground.pending_conversation_deletions')")
            page.get_by_test_id('retry-conversation-delete').click()
            playwright_api.expect(page.get_by_test_id('pending-conversation-delete')).to_have_count(0)
            assert errors == []
        finally:
            browser.close()
