"""Keep Chat usable within a viewport while content scrolls internally."""

from test_playground_browser import _launch_browser, local_playground, playwright_api
from test_playground_browser_layout import _observed_page


def _chat_geometry(page, *, fill_messages=False):
    return page.evaluate('''(fillMessages) => {
      const rect = (selector) => {
        const box = document.querySelector(selector).getBoundingClientRect();
        return { top: box.top, bottom: box.bottom, left: box.left, right: box.right };
      };
      const messages = document.querySelector('#chat-messages');
      if (fillMessages && messages.scrollHeight <= messages.clientHeight) {
        for (let index = 0; index < 24; index += 1) {
          const item = document.createElement('div');
          item.className = 'message is-assistant';
          item.textContent = `Long conversation item ${index}: ` + 'Details '.repeat(30);
          messages.append(item);
        }
      }
      return {
        viewport: { width: innerWidth, height: innerHeight },
        documentOverflowX: document.documentElement.scrollWidth - innerWidth,
        documentOverflowY: document.documentElement.scrollHeight - innerHeight,
        composer: rect('#chat-form'), send: rect('#chat-send'),
        provider: rect('#chat-provider'), model: rect('#chat-model'),
        rail: rect('.conversation-rail'), messages: rect('#chat-messages'),
        messagesOverflow: messages.scrollHeight - messages.clientHeight,
      };
    }''', fill_messages)


def _assert_in_viewport(box, viewport):
    assert box['top'] >= -1, (box, viewport)
    assert box['bottom'] <= viewport['height'] + 1, (box, viewport)
    assert box['left'] >= -1, (box, viewport)
    assert box['right'] <= viewport['width'] + 1, (box, viewport)


def test_chat_keeps_route_rail_and_composer_visible_at_desktop_and_mobile(local_playground):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.get_by_test_id('nav-chat').click()
            page.get_by_test_id('chat-provider').select_option('codex')
            page.get_by_test_id('chat-model').select_option('fixture/openai-model')
            page.get_by_test_id('chat-input').fill('Check the compact chat layout')
            page.get_by_test_id('chat-send').click()
            page.get_by_test_id('chat-messages').get_by_text('Browser fixture answer').wait_for(
                timeout=15000)
            page.locator('#conversation-count').get_by_text('1').wait_for()
            playwright_api.expect(page.locator('#new-conversation')).to_be_enabled(
                timeout=15000)

            for width, height in [(1440, 900), (390, 844), (320, 700)]:
                page.set_viewport_size({'width': width, 'height': height})
                geometry = _chat_geometry(page, fill_messages=True)
                assert geometry['documentOverflowX'] <= 1, geometry
                assert geometry['documentOverflowY'] <= 1, geometry
                for key in ['composer', 'send', 'provider', 'model', 'rail', 'messages']:
                    _assert_in_viewport(geometry[key], geometry['viewport'])
                assert geometry['messagesOverflow'] > 0, geometry

                page.locator('#route-settings summary').click()
                account = page.get_by_test_id('chat-account')
                assert account.is_visible()
                _assert_in_viewport(_chat_geometry(page)['composer'], geometry['viewport'])
                page.keyboard.press('Escape')
                assert not page.locator('#route-settings').evaluate('(element) => element.open')

            assert errors == []
        finally:
            browser.close()
