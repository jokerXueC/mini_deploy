import http.client
import json
import time

import pytest

import agent
from server_performance import History
from test_server_performance import sample
from test_server_performance_browser import demo as demo
from test_ssh_browser import page as page, playwright
from test_deployment_removal import runtime as runtime, http_server as http_server


def request(server, tag='', authenticated=True):
    headers = {'If-None-Match': tag} if tag else {}
    if authenticated:
        headers['Cookie'] = f'{agent.COOKIE_NAME}={server.cookie}'
    connection = http.client.HTTPConnection(server.origin.removeprefix('http://'))
    connection.request('GET', '/performance/server?period=24h&live=1', headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_conditional_poll_skips_full_queries_and_detects_saved_samples(http_server, runtime, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(agent.time, 'time', lambda: clock[0])
    history = History(runtime)
    monkeypatch.setattr(agent, '_server_performance', lambda: history)
    history.observe(sample(20), clock[0])
    history.flush(clock[0], force=True)
    status, headers, body = request(http_server)
    assert status == 200 and json.loads(body)['context_details'] == []
    tag = headers['ETag']
    original = history.query
    monkeypatch.setattr(history, 'query', lambda *args: pytest.fail('unchanged poll must skip full history'))
    status, _, body = request(http_server, tag)
    assert status == 304 and body == b''
    assert request(http_server, tag, False)[0] == 401
    monkeypatch.setattr(history, 'query', original)
    clock[0] += 1
    history.observe(sample(100), clock[0])
    history.flush(clock[0], force=True)
    status, headers, body = request(http_server, tag)
    assert status == 200 and headers['ETag'] != tag
    assert json.loads(body)['summary']['cpu_percent']['latest'] == 100


def test_live_changes_include_process_scenes_write_errors_and_rolling_period(runtime):
    history = History(runtime)
    now = int(time.time() // 30) * 30 + 1
    first = history.version('24h', now)
    history.record_context({'at': now, 'available': False, 'warnings': [], 'processes': []}, sample())
    history.flush(now, force=True)
    second = history.version('24h', now)
    assert second != first
    history.error = 'write failed'
    assert history.version('24h', now) != second
    assert history.version('24h', now + 30) != history.version('24h', now)
    assert history.version('1h', now) != history.version('24h', now)


@pytest.mark.parametrize('width', [1440, 390])
def test_automatic_poll_updates_existing_canvas_preserves_reading_and_stops_when_paused(page, http_server, demo, monkeypatch, tmp_path, width):
    page.set_viewport_size({'width': width, 'height': 900})
    clock = [time.time()]
    monkeypatch.setattr(agent.time, 'time', lambda: clock[0])
    page.clock.install()
    polls = []
    # Track the report's 24h scope; the homepage resumes its separate 1h scope on close.
    page.on('request', lambda req: polls.append(req.url)
            if '/performance/server?' in req.url and 'period=24h' in req.url else None)
    page.goto(http_server.origin + '/ui?view=server')
    page.locator('#serverPerformanceOpen').click()
    frame = page.frame_locator('#serverPerformanceFrame')
    playwright.expect(frame.locator('#summary')).to_contain_text('CPU', timeout=15000)
    frame.locator('canvas').first.evaluate('(canvas) => { window.originalCanvas = canvas; window.beforePixels = Array.from(canvas.getContext("2d").getImageData(0,0,canvas.width,canvas.height).data).join(","); window.liveMarker = 1; }')
    original_url = page.locator('#serverPerformanceFrame').get_attribute('src')
    frame.locator('#sceneAbnormal').check()
    frame.locator('#sceneRows button').first.click()
    frame.locator('.scene-detail:not([hidden]) summary').first.click()
    frame.locator('.scene-detail:not([hidden])').scroll_into_view_if_needed()
    scroll = frame.locator('body').evaluate('() => scrollY')
    clock[0] += 1
    demo.observe(sample(100), clock[0])
    demo.flush(clock[0], force=True)
    # A rolling 24-hour window can expire old samples as new ones arrive.
    expected_count = demo.query(now=clock[0])['sample_seconds']
    page.clock.run_for(15500)
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text(f'{expected_count} 次有效采样')
    page.clock.run_for(550)
    assert page.locator('#serverPerformanceFrame').get_attribute('src') == original_url
    assert frame.locator('body').evaluate('() => window.liveMarker') == 1
    assert frame.locator('canvas').first.evaluate('(canvas) => canvas === window.originalCanvas')
    assert frame.locator('canvas').first.evaluate('(canvas) => Array.from(canvas.getContext("2d").getImageData(0,0,canvas.width,canvas.height).data).join(",") !== window.beforePixels')
    assert frame.locator('#sceneAbnormal').is_checked()
    assert frame.locator('.scene-detail:not([hidden]) details[open]').count() == 1
    assert abs(frame.locator('body').evaluate('() => scrollY') - scroll) <= 3
    page.screenshot(path=str(tmp_path / f'performance-live-{width}.png'))
    page.locator('#serverPerformanceAuto').uncheck()
    saved_polls = len(polls)
    page.clock.run_for(45000)
    assert len(polls) == saved_polls
    page.locator('#serverPerformanceAuto').check()
    playwright.expect(page.locator('#serverPerformanceRefresh')).to_be_enabled()
    page.locator('#serverPerformanceClose').click()
    saved_polls = len(polls)
    page.clock.run_for(45000)
    assert len(polls) == saved_polls


def test_background_poll_errors_keep_report_and_hidden_page_stops_polling(page, http_server, demo, monkeypatch):
    page.clock.install()
    page.goto(http_server.origin + '/ui?view=server')
    page.locator('#serverPerformanceOpen').click()
    frame = page.frame_locator('#serverPerformanceFrame')
    playwright.expect(frame.locator('#summary')).to_contain_text('CPU', timeout=15000)
    url = page.locator('#serverPerformanceFrame').get_attribute('src')
    original = demo.query
    monkeypatch.setattr(demo, 'query', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    page.clock.run_for(15500)
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('保留当前图表')
    assert page.locator('#serverPerformanceFrame').get_attribute('src') == url
    monkeypatch.setattr(demo, 'query', original)
    page.evaluate("Object.defineProperty(document, 'hidden', {get: () => true, configurable: true}); document.dispatchEvent(new Event('visibilitychange'))")
    polls = []
    page.on('request', lambda req: polls.append(req.url) if '/performance/server?' in req.url else None)
    page.clock.run_for(45000)
    assert not polls
    page.evaluate("Object.defineProperty(document, 'hidden', {get: () => false, configurable: true}); document.dispatchEvent(new Event('visibilitychange'))")
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('已更新')


def test_open_scene_survives_leaving_the_rolling_range_and_manual_refresh_keeps_period(page, http_server, demo):
    page.goto(http_server.origin + '/ui?view=server')
    page.locator('#serverPerformanceOpen').click()
    frame = page.frame_locator('#serverPerformanceFrame')
    playwright.expect(frame.locator('#summary')).to_contain_text('CPU', timeout=15000)
    page.locator('[data-server-period="1h"]').click()
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('已读取')
    frame.locator('#sceneRows button').first.click()
    playwright.expect(frame.locator('.scene-detail:not([hidden])')).to_contain_text('aimore-cloud')
    original_url = page.locator('#serverPerformanceFrame').get_attribute('src')
    data = demo.query('1h')
    data['contexts'], data['context_details'] = [], []
    page.evaluate("next => document.getElementById('serverPerformanceFrame').contentWindow.postMessage({type:'performance-report-update', data:next}, '*')", data)
    playwright.expect(frame.locator('[data-held-scene]')).to_contain_text('保留当前展开的现场')
    playwright.expect(frame.locator('.scene-detail:not([hidden])')).to_contain_text('aimore-cloud')
    page.locator('#serverPerformanceRefresh').click()
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('已更新')
    assert page.locator('[data-server-period="1h"]').get_attribute('aria-pressed') == 'true'
    assert page.locator('#serverPerformanceFrame').get_attribute('src') == original_url
