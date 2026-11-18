"""Rich container evidence in the existing panel; all APIs are simulated."""
import pytest

from scripts.preview_server_overview import container_history
from test_maintenance_performance_browser import preview, playwright
from test_monitoring_browser import dashboard

ID = 'a' * 64


@pytest.mark.parametrize('width,theme', [(1440, 'dark'), (1440, 'light'), (390, 'dark'), (390, 'light')])
def test_resource_categories_units_peaks_and_responsive_layout(preview, tmp_path, width, theme):
    page, _, _ = preview
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    payload = container_history(ID, '24h')
    payload['processes'][0]['name'] = '<img src=x onerror=alert(1)>'
    payload['events'][0]['title'] = '<script>unsafe</script>'
    page.route('**/performance/containers?*', lambda route: route.fulfill(json=payload))
    page.locator('[data-performance-container]').click()
    playwright.expect(page.locator('#performanceCharts svg')).to_have_count(4)
    playwright.expect(page.locator('#containerCurrentMetrics')).to_contain_text('内存余量')
    playwright.expect(page.locator('#containerCurrentMetrics')).to_contain_text('MiB')
    playwright.expect(page.locator('#containerEvidence')).to_contain_text('4.2 ms')
    assert page.locator('#performanceCharts .container-peak-line').first.get_attribute('d')
    for pane, text, charts in [('network', 'KiB/s', 2), ('storage', '当前日志段大小', 5),
                               ('processes', '<img src=x onerror=alert(1)>', 4),
                               ('events', '<script>unsafe</script>', 0)]:
        page.locator(f'[data-container-pane="{pane}"]').click()
        playwright.expect(page.locator('#performanceCharts svg')).to_have_count(charts)
        playwright.expect(page.locator('#performanceDialog')).to_contain_text(text)
        assert page.locator('#containerEvidence img, #containerEvidence script').count() == 0
        assert page.locator('#performanceDialog').evaluate('(e) => e.scrollWidth <= e.clientWidth')
    page.locator('[data-container-pane="resources"]').click()
    page.wait_for_timeout(400)
    bounds = page.locator('#performanceDialog').bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1
    assert bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= 901
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'container-{width}-{theme}.png'), animations='disabled')


def test_period_loading_does_not_mislabel_previous_charts_and_refresh_stops_on_close(preview):
    page, _, _ = preview
    control = {'calls': 0, 'held': None}
    def api(route):
        control['calls'] += 1
        if 'period=7d' in route.request.url:
            control['held'] = route
            return
        route.fulfill(json=container_history(ID, '24h'))
    page.route('**/performance/containers?*', api)
    page.clock.install()
    page.locator('[data-performance-container]').click()
    playwright.expect(page.locator('#performanceCharts svg')).to_have_count(4)
    before = control['calls']
    page.clock.run_for(16000)
    playwright.expect(page.locator('#performanceFeedback')).to_contain_text('自动更新')
    assert control['calls'] > before
    page.locator('[data-performance-period="7d"]').click()
    playwright.expect(page.locator('#performanceFeedback')).to_contain_text('读取')
    playwright.expect(page.locator('#performanceCharts svg')).to_have_count(0)
    page.wait_for_timeout(50)
    assert control['held'] is not None
    control['held'].fulfill(json=container_history(ID, '7d'))
    playwright.expect(page.locator('#performanceCharts svg')).to_have_count(4)
    page.locator('[data-performance-close]').click()
    before = control['calls']
    page.clock.run_for(31000)
    assert control['calls'] == before
