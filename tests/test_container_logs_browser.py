"""Live log following, stale-response cancellation and theme-aware modal layout."""
import pytest

from test_ssh_browser import page as page, playwright
from test_deployment_removal import http_server as http_server, runtime as runtime


@pytest.fixture
def logs(page, http_server):
    page.clock.install()
    state = {'rows': ['INFO entry ' + str(index) for index in range(80)], 'calls': 0, 'fail': False}
    def respond(route):
        state['calls'] += 1
        if state['fail']:
            route.fulfill(status=503, json={'detail': '日志暂不可读'})
        else:
            route.fulfill(json={'container': 'api-service', 'lines': state['rows']})
    page.route('**/docker/logs?*', respond)
    page.goto(http_server.origin + '/ui?view=events')
    page.evaluate('showContainerLogs("api-service")')
    playwright.expect(page.locator('#containerLogsContent')).to_contain_text('INFO entry 79')
    return state


def test_poll_appends_without_replacing_selection_and_freezes_history_scroll(page, logs):
    original = page.locator('#containerLogsContent .log-line').first
    original.evaluate('el => window.savedLogNode = el')
    logs['rows'].append('INFO live entry')
    page.clock.fast_forward(2100)
    playwright.expect(page.locator('#containerLogsContent')).to_contain_text('INFO live entry')
    assert page.evaluate('savedLogNode === document.querySelector("#containerLogsContent .log-line")')
    page.locator('#containerLogsContent').evaluate('el => { el.scrollTop = 0; el.dispatchEvent(new Event("scroll")); }')
    logs['rows'].append('WARN while reading history')
    page.clock.fast_forward(2100)
    playwright.expect(page.locator('#containerLogLiveStatus')).to_contain_text('正在查看历史')
    playwright.expect(page.locator('#containerLogsContent')).not_to_contain_text('WARN while reading history')
    assert page.locator('#containerLogsContent').evaluate('el => el.scrollTop') == 0
    page.locator('#containerLogFollow').click()
    playwright.expect(page.locator('#containerLogsContent')).to_contain_text('WARN while reading history')
    page.locator('#containerLogPause').click()
    calls = logs['calls']
    page.clock.fast_forward(6100)
    assert logs['calls'] == calls
    page.locator('#containerLogPause').click()
    playwright.expect(page.locator('#containerLogLiveStatus')).to_contain_text('已更新')
    page.locator('#closeContainerLogsBtn').click()
    calls = logs['calls']
    page.clock.fast_forward(6100)
    assert logs['calls'] == calls


def test_retries_keep_snapshot_escape_content_and_closed_modal_stays_closed(page, logs):
    logs['fail'] = True
    page.clock.fast_forward(2100)
    playwright.expect(page.locator('#containerLogLiveStatus')).to_contain_text('稍后重试')
    playwright.expect(page.locator('#containerLogsContent')).to_contain_text('INFO entry 79')
    logs['fail'] = False
    logs['rows'] = ['<img src=x onerror=alert(1)>', 'ERROR actual error', 'repeat', 'repeat']
    page.clock.fast_forward(2100)
    playwright.expect(page.locator('#containerLogsContent .log-line')).to_have_count(4)
    assert page.locator('#containerLogsContent img').count() == 0
    assert page.locator('#containerLogsContent').inner_text().count('repeat') == 2
    page.route('**/docker/logs?*', lambda route: route.abort('failed'))
    page.evaluate('showContainerLogs("other-service"); closeContainerLogsModal();')
    page.clock.fast_forward(2100)
    playwright.expect(page.locator('#containerLogsModal')).to_be_hidden()


@pytest.mark.parametrize('width,theme,height', [(1440, 'light', 1000), (390, 'light', 1000), (320, 'dark', 1000), (320, 'dark', 568)])
def test_log_layout_theme_and_controls_fit_viewport(page, logs, width, theme, height, tmp_path):
    page.set_viewport_size({'width': width, 'height': height})
    page.add_style_tag(content='body, .logs { transition: none !important; }')
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    modal = page.locator('.container-log-modal')
    box = modal.bounding_box()
    assert box['x'] >= 0 and box['x'] + box['width'] <= width
    assert box['y'] >= 0 and box['y'] + box['height'] <= height + 1
    assert page.locator('#containerLogsContent').evaluate('el => getComputedStyle(el).backgroundColor') == page.locator('body').evaluate('el => getComputedStyle(el).backgroundColor')
    page.screenshot(path=str(tmp_path / ('live-logs-' + str(width) + '-' + theme + '.png')), full_page=True)
