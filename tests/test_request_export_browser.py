"""Download complete data and inspect offline charts on desktop and mobile."""
import csv
import io
import zipfile

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
