"""Request origins use full history and remain usable under filters and on mobile."""
import pytest

import agent
from scripts.preview_request_export import seed
from test_ssh_browser import page as page, playwright
from test_deployment_removal import http_server as http_server, runtime as runtime


@pytest.fixture
def demo(runtime, monkeypatch):
    history = seed(runtime, map_locations=True)
    monkeypatch.setattr(agent, '_history_instance', history)
    return history


def test_request_map_full_history_details_filters_refresh_and_responsive_layout(page, http_server, demo, tmp_path):
    page.goto(http_server.origin + '/ui?view=requests')
    playwright.expect(page.locator('#requestMapNote')).to_contain_text('1840 条明细')
    playwright.expect(page.locator('#requestMapNote')).to_contain_text('24 个 IP')
    playwright.expect(page.locator('#requestMapDots > g')).to_have_count(4)
    playwright.expect(page.locator('#requestMapLand path').first).to_be_visible()
    point = page.locator('#requestMapDots > g').first
    point.focus()
    page.keyboard.press('Enter')
    playwright.expect(page.locator('#requestMapDetail')).to_be_visible()
    playwright.expect(page.locator('#requestMapDetailBody')).to_contain_text('6 个 IP')
    playwright.expect(page.locator('#requestMapDetailBody')).to_contain_text('8.8.8.')
    playwright.expect(page.locator('#requestMapDetailBody')).to_contain_text('api.demo.local')
    # A fresh poll must preserve the selected region and keyboard focus.
    page.evaluate('''() => { window.RequestMap.render = new Proxy(window.RequestMap.render, {apply(fn, self, args) {
        window.mapPolls = (window.mapPolls || 0) + 1; return Reflect.apply(fn, self, args); }}); }''')
    page.locator('#requestRefresh').click()
    page.wait_for_function('window.mapPolls > 0')
    playwright.expect(page.locator('#requestMapDetail')).to_be_visible()
    page.locator('#requestMapDetailClose').click()
    playwright.expect(point).to_be_focused()
    for theme, width in [('light', 1440), ('dark', 390)]:
        page.evaluate('(theme) => applyTheme(theme)', theme)
        page.set_viewport_size({'width': width, 'height': 960})
        page.locator('#requestMapTitle').scroll_into_view_if_needed()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('.request-map-section').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        page.screenshot(path=str(tmp_path / f'request-map-{theme}-{width}.png'), full_page=True, animations='disabled')
    page.locator('#requestMapRegions [data-region="all"]').click()
    page.locator('[data-request-ip="8.8.8.1"]').click()
    playwright.expect(page.locator('#requestSearch')).to_have_value('8.8.8.1')
    # Text search also matches addresses .10-.19; all matched IPs must remain visible.
    playwright.expect(page.locator('#requestMapNote')).to_contain_text('11 个 IP')
    page.locator('#requestSearch').fill('8.8.8.24')
    playwright.expect(page.locator('#requestMapNote')).to_contain_text('1 个 IP')
    page.locator('#requestStatus').select_option('error', force=True)
    playwright.expect(page.locator('#requestMapNote')).to_contain_text('6 条明细')
    page.locator('#requestPeriod').select_option('1h', force=True)
    playwright.expect(page.locator('#requestMapNote')).to_contain_text('最近 1 小时')


def test_live_map_counts_only_filtered_log_samples_and_keeps_escaped_detail(page, http_server):
    records = [
        {'client_ip': '::ffff:8.8.8.8', 'status': 200, 'at': '2026-10-24T00:00:00Z', 'method': 'GET', 'path': '/ok'},
        {'client_ip': '8.8.8.8', 'status': 503, 'at': '2026-10-24T00:00:01Z', 'method': 'GET', 'path': '/<script>bad</script>'},
        {'status': 200, 'at': '2026-10-24T00:00:02Z', 'method': 'GET', 'path': '/unknown'},
    ]
    page.route('**/request-gateways', lambda route: route.fulfill(json={'entries': [], 'history_sources': []}))
    page.route('**/nginx-requests?*', lambda route: route.fulfill(json={'mode': 'nginx', 'enabled': True,
        'records': records, 'visitors': {'items': [{'ip': '8.8.8.8', 'aliases': ['::ffff:8.8.8.8'],
            'location': {'label': 'test', 'latitude': 31, 'longitude': 121}}]}}))
    page.goto(http_server.origin + '/ui?view=requests')
    page.locator('#requestBackend').select_option('nginx', force=True)
    playwright.expect(page.locator('#requestMapNote')).to_contain_text('3 条明细')
    playwright.expect(page.locator('#requestMapNote')).to_contain_text('1 条未采集 IP')
    page.locator('#requestMapDots > g').click()
    playwright.expect(page.locator('#requestMapDetailBody')).to_contain_text('2 次请求')
    playwright.expect(page.locator('#requestMapDetailBody')).to_contain_text('/<script>bad</script>')
    assert page.locator('#requestMapDetailBody script').count() == 0
    page.locator('#requestStatus').select_option('error', force=True)
    playwright.expect(page.locator('#requestMapNote')).to_contain_text('1 条明细')
    playwright.expect(page.locator('#requestMapDetailBody')).to_contain_text('1 次请求')
