"""A page/server contract mismatch must preserve drafts and never submit work."""

import json
from urllib.parse import urlsplit

import pytest

from playground.server import API_REVISION
from test_playground_browser import _launch_browser, _page, local_playground, playwright_api


def _choose_route(page):
    page.get_by_test_id('nav-chat').click()
    page.get_by_test_id('chat-provider').select_option('codex')
    page.get_by_test_id('chat-model').select_option('fixture/openai-model')


def _wait_for_answers(page, count):
    playwright_api.expect(page.get_by_test_id('chat-messages').get_by_text(
        'Browser fixture answer', exact=True)).to_have_count(count, timeout=15000)
    playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(timeout=15000)


def _instance_mutations(page):
    requests = []

    def record(request):
        path = urlsplit(request.url).path
        if request.method in {'POST', 'DELETE'} and path.startswith('/api/instances'):
            requests.append({'path': path, 'body': request.post_data_json,
                             'revision': request.headers.get('x-agentbridge-api-revision')})

    page.on('request', record)
    return requests


def _wait_for_rejection(page, draft):
    page.locator('#toast-region').get_by_text('playground_update_required').wait_for()
    playwright_api.expect(page.get_by_test_id('chat-send')).to_be_enabled()
    playwright_api.expect(page.get_by_test_id('chat-input')).to_have_value(draft)
    page.wait_for_load_state('networkidle')


@pytest.mark.parametrize('revision', [None, 99], ids=['missing-revision', 'different-revision'])
@pytest.mark.parametrize('stage', ['create', 'update', 'message'])
def test_incompatible_meta_preserves_draft_until_explicit_retry(local_playground, revision, stage):
    bridge = local_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            _choose_route(page)
            initial = None
            if stage != 'create':
                page.get_by_test_id('chat-input').fill('Establish this fixture conversation')
                page.get_by_test_id('chat-send').click()
                _wait_for_answers(page, 1)
                initial = bridge.instances()[0]
            if stage == 'update':
                page.get_by_test_id('chat-routing-mode').select_option('pinned')

            metadata = page.request.get(f"{local_playground['url']}api/meta").json()
            if revision is None:
                metadata['result'].pop('api_revision', None)
            else:
                metadata['result']['api_revision'] = revision

            def incompatible_meta(route):
                route.fulfill(status=200, content_type='application/json',
                              body=json.dumps(metadata))

            page.route('**/api/meta', incompatible_meta)
            mutations = _instance_mutations(page)
            draft = f'Preserve this {stage} draft during the upgrade'
            page.get_by_test_id('chat-input').fill(draft)
            page.get_by_test_id('chat-send').click()
            _wait_for_rejection(page, draft)
            assert mutations == []
            if initial:
                unchanged = bridge.instance_get(initial['id'])
                assert unchanged['version'] == initial['version']
                assert unchanged['native_session_id'] == initial['native_session_id']
                assert len(bridge.turns(instance_id=initial['id'])) == 1
            else:
                assert bridge.instances() == []
                assert not local_playground['capture'].exists()

            page.unroute('**/api/meta', incompatible_meta)
            page.get_by_test_id('chat-send').click()
            _wait_for_answers(page, 2 if initial else 1)
            instances = bridge.instances()
            assert len(instances) == 1
            instance = instances[0]
            paths = [request['path'] for request in mutations]
            message_path = f"/api/instances/{instance['id']}/messages"
            expected_paths = ([f"/api/instances/{instance['id']}"] if stage == 'update'
                              else ['/api/instances'] if stage == 'create' else [])
            assert paths == [*expected_paths, message_path]
            assert all(request['revision'] == str(API_REVISION) for request in mutations)
            assert mutations[-1]['body']['content'] == draft
            assert len(bridge.turns(instance_id=instance['id'])) == (2 if initial else 1)
            if initial:
                assert instance['id'] == initial['id']
                assert instance['native_session_id'] == initial['native_session_id']
            if stage == 'update':
                assert instance['routing_mode'] == 'pinned'
            assert errors == []
        finally:
            browser.close()


def test_server_revision_change_after_preflight_does_not_resend_automatically(local_playground):
    bridge = local_playground['bridge']
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _page(browser, local_playground['url'])
            _choose_route(page)
            mutations = _instance_mutations(page)

            def upgraded_backend(route):
                if route.request.method == 'POST':
                    route.fulfill(status=409, content_type='application/json',
                                  body=json.dumps({'error': {
                                      'code': 'playground_update_required',
                                      'message': 'Fixture server changed after the compatibility check.'}}))
                else:
                    route.continue_()

            page.route('**/api/instances/*/messages', upgraded_backend)
            draft = 'Submit this only after my explicit retry'
            page.get_by_test_id('chat-input').fill(draft)
            page.get_by_test_id('chat-send').click()
            _wait_for_rejection(page, draft)
            instance = bridge.instances()[0]
            message_path = f"/api/instances/{instance['id']}/messages"
            assert [request['path'] for request in mutations] == ['/api/instances', message_path]
            assert bridge.turns(instance_id=instance['id']) == []
            assert not local_playground['capture'].exists()
            first_submission = mutations[-1]['body']

            page.unroute('**/api/instances/*/messages', upgraded_backend)
            page.get_by_test_id('chat-send').click()
            _wait_for_answers(page, 1)
            assert [request['path'] for request in mutations] == [
                '/api/instances', message_path, message_path]
            assert mutations[-1]['body'] == first_submission
            assert all(request['revision'] == str(API_REVISION) for request in mutations)
            assert len(bridge.instances()) == 1
            assert len(bridge.turns(instance_id=instance['id'])) == 1
            assert errors == []
        finally:
            browser.close()
