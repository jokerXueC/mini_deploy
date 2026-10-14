import csv
import io
import zipfile

import pytest

import agent
import server_performance
from scripts.preview_server_performance import seed
from test_ssh_browser import page as page, playwright
from test_deployment_removal import http_server as http_server, runtime as runtime


@pytest.fixture
def demo(runtime, monkeypatch):
    history = seed(runtime)
    monkeypatch.setattr(agent, '_server_performance', lambda: history)
    return history


def test_process_scenes_lazy_details_chart_selection_and_offline_exports(page, http_server, demo, tmp_path):
    page.goto(http_server.origin + '/ui?view=server')
    page.locator('#serverPerformanceOpen').click()
    frame = page.frame_locator('#serverPerformanceFrame')
    playwright.expect(frame.locator('#sceneRows > tr:not(.scene-detail)')).to_have_count(20, timeout=15000)
    frame.locator('#sceneRows button').first.click()
    playwright.expect(frame.locator('.scene-detail:not([hidden])')).to_contain_text('aimore-cloud')
    playwright.expect(frame.locator('.scene-detail:not([hidden])')).to_contain_text('变化')
    frame.locator('.scene-detail:not([hidden]) summary').first.click()
    playwright.expect(frame.locator('.scene-detail:not([hidden]) details[open]')).to_contain_text('可执行文件')
    # Navigate beyond embedded recent details to exercise the authenticated parent bridge.
    for _ in range(8):
        frame.locator('#sceneNext').click()
    frame.locator('#sceneRows button').first.click()
    playwright.expect(frame.locator('.scene-detail:not([hidden])')).to_contain_text('postgresql.service', timeout=15000)
    frame.locator('#sceneAbnormal').check()
    playwright.expect(frame.locator('#sceneRows')).to_contain_text('CPU 高占用')
    frame.locator('canvas').first.click(position={'x': 220, 'y': 90})
    playwright.expect(frame.locator('.scene-detail:not([hidden])')).to_contain_text('python', timeout=15000)
    frame.locator('.scene-detail:not([hidden])').scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / 'performance-process-desktop.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    frame.locator('.scene-detail:not([hidden])').scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / 'performance-process-mobile.png'))
    assert frame.locator('body').evaluate('(el) => el.scrollWidth <= innerWidth + 1')
    assert frame.locator('#sceneRows > tr:not(.scene-detail) button').first.evaluate('(el) => el.getBoundingClientRect().right <= innerWidth')
    with page.expect_download() as download:
        page.locator('#serverPerformanceExport').click()
    with zipfile.ZipFile(download.value.path()) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read('processes.csv').decode('utf-8-sig'))))
        assert len(rows) > 5600 and any(row['容器'] == 'aimore-cloud' for row in rows)
        path = tmp_path / 'process-report.html'
        path.write_bytes(archive.read('report.html'))
    page.goto(path.as_uri())
    page.locator('#sceneRows button').first.click()
    playwright.expect(page.locator('.scene-detail:not([hidden])')).to_contain_text('aimore-cloud')


def test_server_report_preview_download_and_offline_charts(page, http_server, demo, tmp_path):
    page.goto(http_server.origin + '/ui?view=server')
    page.locator('#serverPerformanceOpen').click()
    frame = page.frame_locator('#serverPerformanceFrame')
    playwright.expect(frame.locator('#server')).to_contain_text('8 个逻辑核', timeout=15000)
    playwright.expect(frame.locator('#coverage')).to_contain_text('覆盖率')
    assert frame.locator('#summary tr').count() == len(server_performance.METRICS)
    assert frame.locator('canvas').count() == 8
    page.screenshot(path=str(tmp_path / 'performance-preview-desktop.png'))
    with page.expect_download() as download:
        page.locator('#serverPerformanceExport').click()
    with zipfile.ZipFile(download.value.path()) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read('performance.csv').decode('utf-8-sig'))))
        assert len(rows) > 2800
        assert sum(int(row['采样秒数']) for row in rows) > 85000
        report_path = tmp_path / 'report.html'
        report_path.write_bytes(archive.read('report.html'))
    report = page.context.new_page()
    errors, external = [], []
    report.on('pageerror', lambda error: errors.append(str(error)))
    report.on('request', lambda req: external.append(req.url) if req.url.startswith(('http:', 'https:')) else None)
    report.goto(report_path.as_uri())
    playwright.expect(report.locator('#summary')).to_contain_text('CPU')
    assert report.evaluate("document.querySelector('canvas').getContext('2d').getImageData(0,0,200,200).data.some((v,i)=>i%4===3 && v>0)")
    report.screenshot(path=str(tmp_path / 'performance-report-desktop.png'), full_page=True)
    report.set_viewport_size({'width': 390, 'height': 844})
    assert report.evaluate('document.documentElement.scrollWidth <= innerWidth')
    report.screenshot(path=str(tmp_path / 'performance-report-mobile.png'), full_page=True)
    assert not errors and not external
    report.close()
    page.locator('[data-server-period="1h"]').click()
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('已读取')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'performance-preview-mobile.png'))
    page.locator('#serverPerformanceClose').click()


def test_empty_history_and_unavailable_export_are_visible(page, http_server, runtime, monkeypatch):
    history = server_performance.History(runtime)
    monkeypatch.setattr(agent, '_server_performance', lambda: history)
    page.add_init_script("if (window === top) localStorage.setItem('mini_deploy-theme', 'dark')")
    page.goto(http_server.origin + '/ui?view=server')
    page.locator('#serverPerformanceOpen').click()
    frame = page.frame_locator('#serverPerformanceFrame')
    playwright.expect(frame.locator('#coverage')).to_contain_text('等待首次采样')
    playwright.expect(frame.locator('html')).to_have_attribute('data-theme', 'dark')
    playwright.expect(frame.locator('.plot-empty').first).to_be_visible()
    monkeypatch.setattr(history, 'query', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    page.locator('#serverPerformanceRefresh').click()
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('生成失败')
    assert page.locator('#serverPerformanceFrame').is_hidden()
