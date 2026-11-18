import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import agent
from performance_metrics import ContainerHistory
from request_history import History as RequestHistory
from server_performance import History
from scripts.preview_server_overview import system
from test_server_performance import sample
from test_ssh_browser import page as page, playwright
from test_deployment_removal import http_server as http_server, runtime as runtime


@pytest.fixture
def observed(page, http_server, runtime, monkeypatch):
    end = int(time.time() // 60) * 60 - 60
    server, docker, requests = History(runtime), ContainerHistory(runtime), RequestHistory(runtime)
    for offset in range(-600, 0):
        server.observe(sample(20 if offset < -180 else 60), end + offset)
    for offset in range(-600, 0, 30):
        docker.record([{'id':'a' * 64, 'name':'api-server', 'cpu_percent':20 if offset < -90 else 70,
                        'memory_percent':50}], end + offset)
    for offset in (-170, -50):
        server.record_context({'at':end+offset, 'available':True, 'warnings':[], 'processes':[
            {'pid':5, 'start_ticks':10, 'name':'python', 'host_cpu_percent':10 if offset < -90 else 50,
             'rss_mib':100 if offset < -90 else 150, 'container_id':'a' * 64, 'container':'api-server'}]},sample())
    server.flush(end,force=True)
    lines = []
    for offset in (-150,-140,-130,-120,-110,-100,-90,-80):
        at = datetime.fromtimestamp(end+offset,timezone.utc).isoformat()
        lines.append(at + ' mini_deploy_req ' + json.dumps({'at':at,'host':'example.com','method':'GET',
            'path':'/slow','duration':'2.0','upstream':'1.8','status':'200'}))
    requests.ingest('api','API', 'test',lines,end,now=end)
    monkeypatch.setattr(agent,'_server_performance',lambda:server)
    monkeypatch.setattr(agent,'_performance_history',lambda:docker)
    monkeypatch.setattr(agent,'_request_history',lambda:requests)
    monkeypatch.setattr(agent,'_monitor',lambda:SimpleNamespace(snapshot=lambda:{'events':[]}))
    payload = system()
    page.route('**/status',lambda route:route.fulfill(json={'system':payload,'agent':{'status':'ok'}}))
    page.route('**/system-metrics',lambda route:route.fulfill(json=payload))
    page.route('**/docker/images',lambda route:route.fulfill(json={'images':[]}))
    page.route('**/system/swap',lambda route:route.fulfill(json={'supported':False,'enabled':False}))
    page.goto(http_server.origin + '/ui?view=server')
    return end


def analyze(page, end):
    local = lambda ts:datetime.fromtimestamp(ts).strftime('%Y-%m-%dT%H:%M')
    page.locator('#observationFrom').fill(local(end-180))
    page.locator('#observationTo').fill(local(end))
    page.locator('#observationRangeForm button[type=submit]').click()
    playwright.expect(page.locator('#observationAnalysis')).to_have_attribute('aria-busy','false')


def test_linked_analysis_comparison_growth_distribution_and_persistent_watches(page, observed):
    analyze(page, observed)
    playwright.expect(page.locator('#observationAnalysisStatus')).to_contain_text('已读取')
    playwright.expect(page.locator('#observationComparison')).to_contain_text('已采集请求')
    playwright.expect(page.locator('#observationGrowth')).to_contain_text('python')
    playwright.expect(page.locator('#observationGrowth')).to_contain_text('+40百分点')
    playwright.expect(page.locator('#observationLatency .latency-bin')).to_have_count(5)
    playwright.expect(page.locator('#observationLatency')).to_contain_text('8 次')
    playwright.expect(page.locator('#observationReferenceField')).not_to_be_visible()
    page.select_option('#observationCompare','custom',force=True)
    playwright.expect(page.locator('#observationReferenceField')).to_be_visible()
    page.locator('[data-growth-metric=memory]').click()
    playwright.expect(page.locator('#observationGrowth')).to_contain_text('+50 MiB')
    page.select_option('#observationCompare','yesterday',force=True)
    playwright.expect(page.locator('#observationComparison')).to_contain_text('不可比')
    page.locator('#observationPin').click()
    assert page.locator('[data-watch-open="0"]').evaluate('el=>{window.retainedWatch=el; return true}')
    page.evaluate('window.ObservationAnalysis.system({server:{cpu_percent:99},docker:{containers:[]}})')
    assert page.locator('[data-watch-open="0"]').evaluate('el=>el===window.retainedWatch')
    page.locator('#observationLatency [data-endpoint-pin]').first.click()
    playwright.expect(page.locator('.observation-watch')).to_have_count(2)
    page.reload()
    playwright.expect(page.locator('.observation-watch')).to_have_count(2)
    playwright.expect(page.locator('.observation-watch').nth(1)).to_contain_text('8 次')
    page.locator('[data-watch-remove="0"]').click()
    playwright.expect(page.locator('.observation-watch')).to_have_count(1)


def test_shift_selection_and_invalid_range(page, observed):
    page.locator('.load-cell').nth(10).click()
    first = page.locator('#observationFrom').input_value()
    page.locator('.load-cell').nth(16).click(modifiers=['Shift'])
    playwright.expect(page.locator('#observationFrom')).to_have_value(first)
    assert page.locator('.load-cell[data-pinned=true]').count() >= 18
    page.locator('#observationFrom').fill('2025-01-01T00:00')
    page.locator('#observationTo').fill('2024-01-01T00:00')
    page.locator('#observationRangeForm button[type=submit]').click()
    playwright.expect(page.locator('#observationRangeError')).to_contain_text('结束晚于开始')


def test_drag_selects_a_linked_range_with_visible_feedback(page, observed):
    start, end = page.locator('.load-cell').nth(68),page.locator('.load-cell').nth(75)
    start.scroll_into_view_if_needed()
    a,b = start.bounding_box(),end.bounding_box()
    page.mouse.move(a['x']+a['width']/2,a['y']+a['height']/2)
    page.mouse.down()
    page.mouse.move(b['x']+b['width']/2,b['y']+b['height']/2,steps=5)
    assert page.locator('.load-cell[data-range-preview=true]').count() >= 24
    page.mouse.up()
    playwright.expect(page.locator('#observationAnalysis')).to_have_attribute('aria-busy','false')
    assert page.locator('.load-cell[data-range-preview=true]').count() == 0
    assert page.locator('.load-cell[data-pinned=true]').count() >= 24
    playwright.expect(page.locator('[data-observation-metric=memory_percent]')).to_have_attribute('aria-pressed','true')


def test_request_insights_use_the_whole_selected_source(page, observed):
    page.route('**/request-gateways',lambda route:route.fulfill(json={'entries':[], 'history_sources':[
        {'key':'api','name':'API','checked':time.time(),'cursor':time.time()}]}))
    page.route('**/request-history?*',lambda route:route.fulfill(json={'history':True,'groups':[], 'total_groups':0,
        'total_requests':0, 'page':0, 'has_more':False}))
    page.evaluate("setView('requests')")
    playwright.expect(page.locator('#requestGatewayKey')).to_have_value('api')
    page.locator('#requestInsights summary').click()
    playwright.expect(page.locator('#requestInsightsBody .latency-bin')).to_have_count(5)
    playwright.expect(page.locator('#requestInsightsBody')).to_contain_text('8 次')
    playwright.expect(page.locator('#requestInsightsBody')).to_contain_text('不受下方搜索与状态筛选影响')


def test_old_analysis_cannot_replace_new_range_and_failure_is_not_empty(page, observed):
    pending = []
    page.route('**/observations/analyze?*',lambda route:pending.append(route))
    page.locator('.load-cell').nth(12).click()
    playwright.expect(page.locator('#observationAnalysis')).to_have_attribute('aria-busy','true')
    assert page.locator('#observationComparison').inner_text() == ''
    page.locator('.load-cell').nth(22).click()
    page.wait_for_function('document.querySelector("#observationAnalysis").getAttribute("aria-busy") === "true"')
    page.wait_for_timeout(100)
    assert len(pending) == 2
    pending[1].fulfill(status=503,json={'detail':'测试：历史暂不可读'})
    playwright.expect(page.locator('#observationAnalysisStatus')).to_contain_text('测试：历史暂不可读')
    pending[0].fulfill(status=503,json={'detail':'过期的旧请求'})
    page.wait_for_timeout(100)
    assert '过期的旧请求' not in page.locator('#observationAnalysisStatus').inner_text()
    page.locator('#observationEvidenceClose').click()
    playwright.expect(page.locator('#observationEvidence')).not_to_be_visible()


@pytest.mark.parametrize('width,theme',[(1440,'light'),(1920,'dark'),(390,'light'),(320,'dark')])
def test_analysis_layout_fits_desktop_and_mobile(page, observed, width, theme, tmp_path):
    page.set_viewport_size({'width':width,'height':1000})
    page.evaluate('(theme)=>applyTheme(theme)',theme)
    analyze(page,observed)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('#observationAnalysis').evaluate('(el)=>el.scrollWidth <= el.clientWidth')
    page.locator('#observationAnalysis').scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / f'analysis-{width}-{theme}.png'))
