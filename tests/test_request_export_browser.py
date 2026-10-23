"""Download complete data and inspect offline charts on desktop and mobile."""
import csv
import io
import zipfile
import time

import pytest

import agent
from scripts.preview_request_export import seed
from test_ssh_browser import page as page, playwright
from test_deployment_removal import http_server as http_server, runtime as runtime


@pytest.fixture
def demo(runtime, monkeypatch):
    history = seed(runtime)
    monkeypatch.setattr(agent, '_history_instance', history)
    return history


def test_expired_connection_distinguishes_old_history_and_releases_only_metadata(page, http_server, runtime, monkeypatch, tmp_path):
    history = seed(runtime, now=time.time() - 2 * 86400)
    history.failure('demo-api', 'API 服务', '代理配置已变化，请求可能绕过采集网关')
    monkeypatch.setattr(agent, '_history_instance', history)
    state = {'released': False, 'actions': []}
    entry = {'key': 'demo-api', 'name': 'API', 'upstream': 'http://api:8766', 'network': 'app_default',
             'state': 'running', 'local_address': '127.0.0.1:18080', 'caddy_upstream': 'mini-gateway-demo-api:10000'}

    def gateways(route):
        connection = {'state': 'not_connected'} if state['released'] else {
            'state': 'connected', 'config_state': 'changed', 'site': 'api.test', 'token': 'test-token',
            'error': '代理配置已变化，请求可能绕过采集网关'}
        route.fulfill(json={'entries': [{**entry, 'connection': connection}], 'history_sources': history.sources()})

    def release(route):
        state['actions'].append(route.request.post_data_json)
        state['released'] = True
        route.fulfill(json={'connection': {'state': 'not_connected'}, 'notice': '旧接入记录已解除，当前代理配置和请求历史保留。'})

    page.route('**/request-gateways', gateways)
    page.route('**/gateway-connections', release)
    page.goto(http_server.origin + '/ui?view=requests')
    playwright.expect(page.locator('#requestSource')).to_contain_text('采集需检查')
    playwright.expect(page.locator('#requestNotice')).to_contain_text('最后一条请求')
    playwright.expect(page.locator('#requestNotice')).to_contain_text('仍保留 1840 条历史')
    playwright.expect(page.locator('#requestRows')).to_contain_text('采集需要检查')
    page.locator('#requestPeriod').select_option('7d', force=True)
    playwright.expect(page.locator('#requestSampleSummary')).to_contain_text('1840')
    page.screenshot(path=str(tmp_path / 'expired-connection-history.png'))
    page.locator('#requestStopMonitoring').click()
    playwright.expect(page.locator('#confirmModal')).to_contain_text('保留当前业务配置')
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#requestStopMonitoring')).to_be_hidden()
    assert state['actions'] == [{'action': 'release', 'key': 'demo-api', 'token': 'test-token', 'confirmed': True}]
    assert history.query('demo-api', period='7d')['total_requests'] == 1840


def test_export_modal_zip_report_preview_and_offline_charts(page, http_server, demo, tmp_path):
    page.goto(http_server.origin + '/ui?view=requests')
    page.locator('#requestExportOpen').click()
    playwright.expect(page.locator('#requestExportDialog')).to_be_visible()
    playwright.expect(page.locator('#requestExportScope')).to_have_value('all')
    page.screenshot(path=str(tmp_path / 'export-dialog-desktop.png'))
    with page.expect_download() as download:
        page.locator('#requestExportDownload').click()
    playwright.expect(page.locator('#requestExportFeedback')).to_contain_text('2480 条请求')
    with zipfile.ZipFile(download.value.path()) as archive:
        detail = list(csv.DictReader(io.StringIO(archive.read('requests.csv').decode('utf-8-sig'))))
        assert len(detail) == 2480
        report_path = tmp_path / 'report.html'
        report_path.write_bytes(archive.read('report.html'))
    page.locator('#requestExportPreview').click()
    frame = page.frame_locator('#requestExportFrame')
    playwright.expect(frame.locator('#totals')).to_contain_text('2,480')
    playwright.expect(frame.locator('#scope')).to_contain_text('全部入口')
    page.screenshot(path=str(tmp_path / 'export-preview-desktop.png'))
    page.locator('#requestExportClose').click()
    report = page.context.new_page()
    errors, network = [], []
    report.on('pageerror', lambda error: errors.append(str(error)))
    report.on('request', lambda req: network.append(req.url) if req.url.startswith(('https:', 'http:')) else None)
    report.goto(report_path.as_uri())
    playwright.expect(report.locator('#totals')).to_contain_text('2,480')
    assert report.locator('#table tr').count() == 12
    assert report.evaluate("document.querySelector('#volume').getContext('2d').getImageData(0,0,200,200).data.some((v,i)=> i%4===3 && v>0)")
    report.locator('#search').fill('/cloud/chat')
    playwright.expect(report.locator('#table tr')).to_have_count(2)
    report.locator('#timelineTab').click()
    playwright.expect(report.locator('#tableCount')).to_contain_text('区间')
    assert report.locator('#table tr').count() > 1
    report.locator('#routesTab').click()
    report.locator('#search').fill('')
    report.screenshot(path=str(tmp_path / 'report-desktop.png'), full_page=True)
    report.set_viewport_size({'width': 390, 'height': 844})
    assert report.evaluate('document.documentElement.scrollWidth <= innerWidth')
    report.screenshot(path=str(tmp_path / 'report-mobile.png'), full_page=True)
    assert not errors and not network
    report.close()


def test_mobile_csv_current_scope_and_empty_report(page, http_server, demo, tmp_path):
    page.goto(http_server.origin + '/ui?view=requests')
    playwright.expect(page.locator('#requestGatewayKey')).to_have_value('demo-api')
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('#requestExportOpen').click()
    page.get_by_role('combobox', name='导出范围').click()
    page.get_by_role('option', name='当前入口 · 全部已保留历史', exact=True).click()
    page.locator('[name=requestExportFormat][value=csv]').check()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'export-dialog-mobile.png'))
    with page.expect_download() as download:
        page.locator('#requestExportDownload').click()
    assert len(list(csv.DictReader(io.StringIO(download.value.path().read_bytes().decode('utf-8-sig'))))) == 1840
    page.locator('#requestExportClose').click()
    page.locator('#requestSearch').fill('no-such-record')
    page.locator('#requestExportOpen').click()
    page.get_by_role('combobox', name='导出范围').click()
    page.get_by_role('option', name='当前入口 · 当前时间和筛选', exact=True).click()
    page.locator('#requestExportPreview').click()
    frame = page.frame_locator('#requestExportFrame')
    playwright.expect(frame.locator('#volumeEmpty')).to_be_visible()
    playwright.expect(frame.locator('#table')).to_contain_text('暂无匹配数据')
    page.screenshot(path=str(tmp_path / 'export-empty-mobile.png'))
