"""Keep every dashboard inside the viewport at desktop and mobile sizes."""

import pytest

from test_playground_browser import _launch_browser, local_playground, playwright_api
from test_playground_browser_layout import _observed_page


VIEWPORTS = ((1440, 900), (390, 844), (320, 700))
VIEW_CONTENT = {
    'overview': ('#metric-accounts', '#capacity-heading',
                 '#overview-capacity-list .capacity-row'),
    'accounts': ('#accounts-add', '#accounts-summary', '#accounts-list',
                 '#accounts-pager', '#accounts-list [data-testid="account-row"]',
                 '#accounts-list [data-testid="account-models-open"]',
                 '#accounts-list [data-testid="remove-account"]'),
    'chat': ('#new-conversation', '#chat-provider', '#chat-model',
             '#chat-messages', '#chat-form', '#chat-send'),
    'activity': ('#activity-title', '#activity-refresh', '#activity-list'),
}
WIDE_SURFACES = {
    'overview': '#view-overview .capacity-surface',
    'accounts': '#accounts-list',
    'chat': '#view-chat .chat-layout',
    'activity': '#view-activity .activity-surface',
}


def _geometry(page, selectors):
    return page.evaluate('''(selectors) => {
      const box = (element) => {
        const rect = element.getBoundingClientRect();
        return { left: rect.left, right: rect.right,
          top: rect.top, bottom: rect.bottom, width: rect.width, height: rect.height };
      };
      const html = document.documentElement;
      const body = document.body;
      return {
        viewport: { width: innerWidth, height: innerHeight },
        overflowX: Math.max(html.scrollWidth, body.scrollWidth) - innerWidth,
        overflowY: Math.max(html.scrollHeight, body.scrollHeight) - innerHeight,
        scroll: { x: scrollX, y: scrollY },
        boxes: Object.fromEntries(selectors.map((selector) =>
          [selector, box(document.querySelector(selector))])),
      };
    }''', selectors)


def _assert_in_viewport(geometry, selector):
    box = geometry['boxes'][selector]
    viewport = geometry['viewport']
    assert box['width'] > 0 and box['height'] > 0, (selector, geometry)
    assert box['left'] >= -1 and box['right'] <= viewport['width'] + 1, (selector, geometry)
    assert box['top'] >= -1 and box['bottom'] <= viewport['height'] + 1, (selector, geometry)


@pytest.mark.parametrize('width,height', VIEWPORTS)
def test_dashboard_views_fit_without_document_scroll(local_playground, width, height):
    with playwright_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page, errors = _observed_page(browser, local_playground['url'])
            page.set_viewport_size({'width': width, 'height': height})
            refresh = page.locator('.sidebar .brand #refresh-button')
            assert refresh.count() == 1 and refresh.is_visible()
            assert page.locator('.workspace .refresh-control').count() == 0
            brand = page.locator('.sidebar .brand').bounding_box()
            button = refresh.bounding_box()
            assert brand and button
            assert brand['x'] - 1 <= button['x']
            assert button['x'] + button['width'] <= brand['x'] + brand['width'] + 1
            assert brand['y'] - 1 <= button['y']
            assert button['y'] + button['height'] <= brand['y'] + brand['height'] + 1

            for view, controls in VIEW_CONTENT.items():
                page.get_by_test_id(f'nav-{view}').click()
                section = page.locator(f'#view-{view}')
                section.wait_for(state='visible')
                for selector in controls:
                    page.locator(selector).first.wait_for(state='visible')
                geometry = _geometry(page, ['#refresh-button', f'#view-{view}',
                                            *controls])
                assert geometry['overflowX'] <= 1, (view, width, height, geometry)
                assert geometry['overflowY'] <= 1, (view, width, height, geometry)
                assert geometry['scroll'] == {'x': 0, 'y': 0}, (view, geometry)
                for selector in ['#refresh-button', f'#view-{view}', *controls]:
                    _assert_in_viewport(geometry, selector)
                surface = page.locator(WIDE_SURFACES[view]).bounding_box()
                workspace = page.locator('.workspace').bounding_box()
                assert surface and workspace
                inset = 30 if width > 700 else 12
                assert surface['x'] - workspace['x'] <= inset, (view, width, surface)
                assert workspace['x'] + workspace['width'] - surface['x'] - surface['width'] <= inset, (view, width, surface)
                if view == 'accounts':
                    list_size = page.locator('#accounts-list').evaluate('''(element) => ({
                      width: element.clientWidth, fullWidth: element.scrollWidth,
                      height: element.clientHeight, fullHeight: element.scrollHeight,
                    })''')
                    assert list_size['fullWidth'] <= list_size['width'] + 1, list_size
                    assert list_size['fullHeight'] <= list_size['height'] + 1, list_size
                    row = page.get_by_test_id('account-row').first
                    assert row.count() == 1
                    assert row.locator('.account-name').inner_text().strip()
                    models = row.get_by_test_id('account-models-open')
                    models.scroll_into_view_if_needed()
                    models.click()
                    page.locator('#account-models-dialog').wait_for(state='visible')
                    page.locator('#account-models-close').click()
                    page.locator('#account-models-dialog').wait_for(state='hidden')
                    assert page.evaluate('({ x: scrollX, y: scrollY })') == {'x': 0, 'y': 0}
            assert errors == []
        finally:
            browser.close()
