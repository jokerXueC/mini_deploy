"""Mobile engines must keep sampled values live, including reduced-motion mode."""
from pathlib import Path
import re
import time

import pytest

import agent
from server_performance import History
from test_server_performance import sample
from test_ssh_browser import playwright
from test_deployment_removal import http_server as http_server, runtime as runtime
from test_server_overview_browser import preview
from scripts.preview_server_overview import system


@pytest.fixture(params=['chromium', 'webkit'])
def mobile_page(request, http_server):
    with playwright.sync_playwright() as runtime:
        engine = getattr(runtime, request.param)
        if not Path(engine.executable_path).exists():
            pytest.skip(f'{request.param} is not installed')
        browser = engine.launch()
        context = browser.new_context(**runtime.devices['Pixel 7' if request.param == 'chromium' else 'iPhone 13'])
        context.add_cookies([{'name': agent.COOKIE_NAME, 'value': http_server.cookie, 'url': http_server.origin}])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/monitoring', lambda route: route.fulfill(json={'targets': [], 'alerts': [], 'events': []}))
        yield page
        assert not errors
        browser.close()


@pytest.mark.parametrize('motion', ['no-preference', 'reduce'])
def test_mobile_samples_refresh_and_reduced_motion_does_not_freeze_values(mobile_page, http_server, motion):
    page = mobile_page
    page.emulate_media(reduced_motion=motion)
    page.clock.install()
    payload = system()
    payload['server'].update(cpu_percent=12, sampled_ts=time.time())
    preview(page, http_server, payload)
    playwright.expect(page.locator('#cpuValue')).to_have_text('12%')
    for number in (41, 73):
        payload['server']['cpu_percent'] = number
        payload['server']['sampled_ts'] += 5
        page.clock.run_for(4500)
        playwright.expect(page.locator('#cpuValue')).to_have_text(f'{number}%')
        assert page.locator('#cpuBar').get_attribute('data-resource-value') == str(number)


def test_mobile_history_updates_and_initial_view_shows_the_latest_samples(mobile_page, http_server, tmp_path):
    page = mobile_page
    page.clock.install()
    now = int(time.time() // 30) * 30
    data = {'interval_seconds': 30, 'until': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
            'contexts': [], 'points': [
                {'ts': now - (119 - i) * 30, 'cpu_percent': 20 + i % 5, 'memory_percent': 55,
                 'network_rx_kib_s': 100, 'network_tx_kib_s': 50} for i in range(120)]}
    preview(page, http_server, system(), realtime=False)
    page.route('**/performance/server?*', lambda route: route.fulfill(json=data))
    page.reload()
    playwright.expect(page.locator('#loadLatest')).to_have_text('24.0%')
    def assert_latest_visible():
        assert page.locator('.load-cell').nth(59).evaluate('''cell => {
        const viewport = cell.closest('.load-map-scroll').getBoundingClientRect();
        const bounds = cell.getBoundingClientRect();
        return bounds.right <= viewport.right + 1 && bounds.left >= viewport.left;
        }'''), 'Mobile viewport must show the newest time cells'
        assert page.locator('.trend-chart-scroll').evaluate_all('''elements => elements.every(
            el => el.scrollWidth - el.clientWidth - el.scrollLeft < 8)''')

    assert_latest_visible()
    playwright.expect(page.locator('.load-cell').nth(59)).to_have_attribute('aria-label', re.compile(r'24\.0%'))
    data['until'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 30))
    data['points'].append({'ts': now + 30, 'cpu_percent': 88, 'memory_percent': 73,
                           'network_rx_kib_s': 400, 'network_tx_kib_s': 200})
    page.clock.run_for(31500)
    playwright.expect(page.locator('#loadLatest')).to_have_text('88.0%')
    playwright.expect(page.locator('.trend-card.cpu .trend-card-head strong')).to_have_text('88%')
    playwright.expect(page.locator('.load-cell').nth(59)).to_have_attribute('data-level', 'high')
    assert_latest_visible()
    for width in (740, 360):
        page.set_viewport_size({'width': width, 'height': 850})
        page.clock.run_for(250)
        assert_latest_visible()
    page.locator('#loadMap').scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / 'mobile-history.png'))
    scrollers = page.locator('.load-map-scroll, .trend-chart-scroll')
    scrollers.evaluate_all('elements => elements.forEach(el => { el.scrollLeft = 0; })')
    page.clock.run_for(250)
    data['points'][-1]['cpu_percent'] = 61
    page.clock.run_for(31500)
    playwright.expect(page.locator('#loadLatest')).to_have_text('61.0%')
    assert scrollers.evaluate_all('elements => elements.every(el => el.scrollLeft === 0)')
    # Returning to the live end resumes following; a new range also starts there.
    scrollers.evaluate_all('elements => elements.forEach(el => { el.scrollLeft = el.scrollWidth; })')
    page.clock.run_for(250)
    data['until'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 60))
    data['points'].append({'ts': now + 60, 'cpu_percent': 91, 'memory_percent': 74,
                           'network_rx_kib_s': 400, 'network_tx_kib_s': 200})
    page.clock.run_for(31500)
    playwright.expect(page.locator('#loadLatest')).to_have_text('91.0%')
    assert_latest_visible()
    scrollers.evaluate_all('elements => elements.forEach(el => { el.scrollLeft = 0; })')
    page.clock.run_for(250)
    page.locator('[data-trend-range="24h"]').click()
    playwright.expect(page.locator('#loadMapRange')).to_have_text('最近 24 小时')
    assert_latest_visible()


def test_mobile_performance_report_updates_canvas_and_resumes_after_background(
        mobile_page, http_server, runtime, monkeypatch, tmp_path):
    page = mobile_page
    clock = [time.time()]
    monkeypatch.setattr(agent.time, 'time', lambda: clock[0])
    history = History(runtime)
    monkeypatch.setattr(agent, '_server_performance', lambda: history)
    for offset, cpu in ((-60, 20), (-30, 40)):
        history.observe(sample(cpu), clock[0] + offset)
    history.flush(clock[0], force=True)
    page.clock.install()
    preview(page, http_server, system(), realtime=False)
    page.unroute('**/performance/server?*')
    page.locator('#serverPerformanceOpen').click()
    frame = page.frame_locator('#serverPerformanceFrame')
    playwright.expect(frame.locator('#summary')).to_contain_text('CPU', timeout=15000)
    canvas = frame.locator('canvas').first
    canvas.evaluate('''canvas => {
        window.originalCanvas = canvas;
        window.originalPixels = canvas.toDataURL();
    }''')
    document = page.locator('#serverPerformanceFrame').get_attribute('srcdoc')
    assert document and 'allow-same-origin' not in page.locator('#serverPerformanceFrame').get_attribute('sandbox')
    for cpu in (88, 65):
        if cpu == 65:
            page.evaluate("Object.defineProperty(document, 'hidden', {value:true, configurable:true}); document.dispatchEvent(new Event('visibilitychange'))")
            page.clock.run_for(45000)
        clock[0] += 30
        history.observe(sample(cpu), clock[0])
        history.flush(clock[0], force=True)
        if cpu == 65:
            page.evaluate("Object.defineProperty(document, 'hidden', {value:false, configurable:true}); document.dispatchEvent(new Event('visibilitychange'))")
        else:
            page.clock.run_for(15500)
        count = history.query(now=clock[0])['sample_seconds']
        playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text(f'{count} 次有效采样')
        page.clock.run_for(1000)
        assert canvas.evaluate('canvas => canvas === window.originalCanvas && canvas.toDataURL() !== window.originalPixels')
        assert page.locator('#serverPerformanceFrame').get_attribute('srcdoc') == document
        canvas.evaluate('canvas => { window.originalPixels = canvas.toDataURL(); }')
    page.screenshot(path=str(tmp_path / 'mobile-performance-report.png'))
    page.locator('#serverPerformanceClose').click()
    page.clock.run_for(250)
    assert page.locator('#serverPerformanceFrame').get_attribute('srcdoc') is None
