"""Large history preview transfer, lazy process details and recoverable loading."""
import base64
import gzip
import http.client
import io
import json
import re

import pytest

import agent
from server_performance import report
from test_server_performance_browser import demo as demo
from test_ssh_browser import page as page, playwright
from test_deployment_removal import http_server as http_server, runtime as runtime


def fetch(server, path, encoding='gzip'):
    connection = http.client.HTTPConnection(server.origin.removeprefix('http://'), timeout=30)
    connection.request('GET', path, headers={'Cookie': f'{agent.COOKIE_NAME}={server.cookie}', 'Accept-Encoding': encoding})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def embedded(raw):
    match = re.search(rb'<script[^>]+id="reportData"[^>]*>([^<]+)</script>', raw)
    assert match
    return json.loads(base64.b64decode(match[1]))


def test_large_preview_omits_embedded_process_details_and_compresses_without_losing_statistics(
        http_server, demo, monkeypatch):
    original = demo.query
    calls = []
    def query(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(demo, 'query', query)
    status, fields, raw = fetch(http_server, '/performance/server/export?period=24h&format=html&preview=1')
    assert status == 200 and fields['Content-Encoding'] == 'gzip'
    assert int(fields['Content-Length']) == len(raw) and fields['Vary'] == 'Accept-Encoding'
    assert fields['X-Performance-Detail-Mode'] == 'on-demand' and fields['Content-Disposition'].startswith('inline;')
    assert calls == [('24h', None, False)]
    data = embedded(gzip.decompress(raw))
    assert data['live_detail'] and data['context_details'] == [] and len(data['contexts']) > 2800
    complete = original('24h')
    assert data['points'] == complete['points'] and data['summary'] == complete['summary']
    assert len(raw) < len(report(complete)) * .4
    # Only expanding a specific scene fetches its complete process list.
    scene = data['contexts'][0]
    status, _, body = fetch(http_server, f"/performance/server/context?run={scene['run']}&ts={scene['ts']}")
    assert status == 200 and json.loads(body)['processes'][0]['container'] == 'aimore-cloud'
    status, fields, body = fetch(http_server, '/performance/server/export?period=24h&format=html')
    assert status == 200 and 'Content-Encoding' not in fields
    assert fields['Content-Disposition'].startswith('attachment;')
    assert embedded(body)['context_details'] and embedded(body)['live_detail'] is False


@pytest.mark.parametrize('encoding', ['identity', 'gzip;q=0', 'gzip;q=invalid'])
def test_preview_respects_clients_that_do_not_accept_gzip(http_server, runtime, monkeypatch, encoding):
    from server_performance import History
    monkeypatch.setattr(agent, '_server_performance', lambda: History(runtime))
    status, fields, body = fetch(http_server, '/performance/server/export?format=html&preview=1', encoding)
    assert status == 200 and 'Content-Encoding' not in fields and embedded(body)['points'] == []


def test_live_refresh_json_is_compressed_and_keeps_existing_contract(http_server, demo):
    status, fields, raw = fetch(http_server, '/performance/server?period=24h&live=1')
    assert status == 200 and fields['Content-Encoding'] == 'gzip'
    assert int(fields['Content-Length']) == len(raw) and fields['ETag']
    data = json.loads(gzip.decompress(raw))
    assert data['period'] == '24h' and data['context_details'] == [] and len(data['points']) > 2800


def test_first_network_failure_retries_automatically_without_erasing_history(page, http_server, demo, tmp_path):
    attempts = []
    def transfer(route):
        attempts.append(route.request.url)
        if len(attempts) == 1:
            route.abort('connectionreset')
        else:
            route.continue_()
    page.clock.install()
    page.route('**/performance/server/export?**', transfer)
    page.goto(http_server.origin + '/ui?view=server')
    page.locator('#serverPerformanceOpen').click()
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('传输失败')
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('历史数据保留')
    page.screenshot(path=str(tmp_path / 'performance-initial-network-failure.png'))
    page.clock.run_for(3500)
    frame = page.frame_locator('#serverPerformanceFrame')
    playwright.expect(frame.locator('#summary')).to_contain_text('CPU', timeout=15000)
    assert len(attempts) == 2
    frame.locator('#sceneRows button').first.click()
    playwright.expect(frame.locator('.scene-detail:not([hidden])')).to_contain_text('aimore-cloud')
    page.screenshot(path=str(tmp_path / 'performance-recovered.png'))


def test_initial_auth_error_is_readable_and_never_retries_forever(page, http_server, runtime):
    attempts = []
    def expired(route):
        attempts.append(route.request.url)
        route.fulfill(status=401, json={'error': 'unauthorized'})
    page.clock.install()
    page.route('**/performance/server/export?**', expired)
    page.goto(http_server.origin + '/ui?view=server')
    page.locator('#serverPerformanceOpen').click()
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('登录已过期')
    page.clock.run_for(60000)
    assert len(attempts) == 1


def test_closing_or_pausing_after_initial_failure_stops_retry(page, http_server, runtime):
    attempts = []
    def interrupted(route):
        attempts.append(route.request.url)
        route.abort('failed')
    page.clock.install()
    page.route('**/performance/server/export?**', interrupted)
    page.goto(http_server.origin + '/ui?view=server')
    page.locator('#serverPerformanceOpen').click()
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('传输失败')
    page.locator('#serverPerformanceAuto').uncheck()
    page.clock.run_for(60000)
    assert len(attempts) == 1
    page.locator('#serverPerformanceClose').click()
    page.clock.run_for(60000)
    assert len(attempts) == 1


def test_http_200_with_unreadable_body_retries_instead_of_leaving_blank_report(page, http_server, runtime, monkeypatch):
    from server_performance import History
    monkeypatch.setattr(agent, '_server_performance', lambda: History(runtime))
    attempts = []
    original = agent.server_performance.build
    def broken_body(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raw = b'incomplete-gzip-body'
            return io.BytesIO(raw), len(raw), 'text/html; charset=utf-8'
        return original(*args, **kwargs)
    monkeypatch.setattr(agent.server_performance, 'build', broken_body)
    page.clock.install()
    page.goto(http_server.origin + '/ui?view=server')
    page.locator('#serverPerformanceOpen').click()
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('传输失败')
    page.clock.run_for(3500)
    playwright.expect(page.frame_locator('#serverPerformanceFrame').locator('#coverage')).to_contain_text('等待首次采样')
    assert len(attempts) == 2
