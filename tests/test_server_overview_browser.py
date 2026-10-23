"""Resource-map data fidelity and the working server workspace at common viewports."""
import time

from test_ssh_browser import page as page, playwright
from test_deployment_removal import http_server as http_server, runtime as runtime
from scripts.preview_server_overview import system


def preview(page, http_server, payload, *, realtime=True):
    page.add_init_script("if (window === top) localStorage.setItem('mini_deploy-theme', 'dark')")
    page.route('**/status', lambda route: route.fulfill(json={'system': payload, 'agent': {'status': 'ok'}}))
    page.route('**/system-metrics', lambda route: route.fulfill(json=payload))
    page.route('**/docker/images', lambda route: route.fulfill(json={'images': []}))
    page.route('**/system/swap', lambda route: route.fulfill(json={'supported': False, 'enabled': False}))
    page.route('**/performance/server?*', lambda route: route.fulfill(json={
        'interval_seconds': 30, 'points': [], 'contexts': []}))
    page.goto(http_server.origin + '/ui?view=server')
    if realtime:
        page.locator('[data-trend-range="realtime"]').click()
    playwright.expect(page.locator('.load-cell')).to_have_count(180)


def test_heatmap_keeps_missing_samples_blank_and_preserves_focus_on_refresh(page, http_server):
    now = int(time.time())
    payload = system()
    payload['realtime_history'] = [
        {'ts': now, 'cpu_percent': 0, 'memory_percent': None, 'network_total_kbps': 0},
        {'ts': now - 1, 'cpu_percent': None, 'memory_percent': 92, 'network_total_kbps': None},
        {'ts': now - 2, 'cpu_percent': 90, 'memory_percent': 40, 'network_total_kbps': 200},
    ]
    preview(page, http_server, payload)
    cells = page.locator('.load-cell')
    playwright.expect(page.locator('#loadAverage')).to_have_text('45.0%')
    playwright.expect(page.locator('#loadHighCount')).to_have_text('1 / 2')
    playwright.expect(cells.nth(57)).to_have_attribute('data-level', 'high')
    playwright.expect(cells.nth(58)).to_have_attribute('data-level', 'empty')
    playwright.expect(cells.nth(59)).to_have_attribute('data-level', 'normal')
    playwright.expect(cells.nth(119)).to_have_attribute('data-level', 'empty')
    cells.nth(58).hover()
    playwright.expect(page.locator('#loadMapDetail')).to_contain_text('无有效采样')
    cells.nth(0).focus()
    page.keyboard.press('End')
    page.keyboard.press('ArrowDown')
    playwright.expect(cells.nth(119)).to_be_focused()
    page.keyboard.press('ArrowUp')
    playwright.expect(cells.nth(59)).to_be_focused()
    playwright.expect(page.locator('#loadMapDetail')).to_contain_text('0.0%')
    # Existing focused elements survive the next poll and show the updated evidence.
    payload['realtime_history'][0]['cpu_percent'] = 20
    playwright.expect(page.locator('#loadAverage')).to_have_text('55.0%', timeout=5000)
    playwright.expect(cells.nth(59)).to_be_focused()
    playwright.expect(page.locator('#loadMapDetail')).to_contain_text('20.0%')


def test_persisted_hour_uses_real_peaks_weighted_means_and_exact_interval_evidence(page, http_server, tmp_path):
    payload = system()
    now = int(time.time() // 60) * 60
    preview(page, http_server, payload, realtime=False)
    calls = []

    def history(route):
        calls.append(route.request.url)
        route.fulfill(json={'interval_seconds': 30, 'until': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
            'points': [{'ts': now - offset, 'cpu_percent': cpu, 'cpu_percent_peak': peak,
                        'memory_percent': None, 'network_rx_kib_s': 0, 'network_tx_kib_s': 0,
                        'weights': {'cpu_percent': weight, 'network_rx_kib_s': 1, 'network_tx_kib_s': 1}}
                       for offset, cpu, peak, weight in ((50, 20, 95, 1), (20, 80, 85, 3))],
            'contexts': [{'at': now - offset, 'ts': now - offset, 'run': 'a' * 32}
                         for offset in (10, 40, 120)]})

    page.route('**/performance/server?*', history)
    page.route('**/performance/server/context?*', lambda route: route.fulfill(json={
        'at': now - 10, 'available': True, 'processes': [
            {'pid': 42, 'name': '<script>bad</script>', 'host_cpu_percent': 70, 'rss_mib': 1024, 'write_kib_s': 12}]}))
    page.route('**/monitoring', lambda route: route.fulfill(json={'events': [
        {'at_ts': now - 10, 'title': '同时段重启', 'detail': '只是一条关联线索'},
        {'at_ts': now - 120, 'title': '不在所选时段'}]}))
    page.locator('[data-trend-range="24h"]').click()
    page.locator('[data-trend-range="1h"]').click()
    playwright.expect(page.locator('#loadAverage')).to_have_text('65.0%')
    playwright.expect(page.locator('#loadPeak')).to_have_text('95.0%')
    playwright.expect(page.locator('#loadHighCount')).to_have_text('2 / 2')
    cell = page.locator('.load-cell').nth(59)
    playwright.expect(cell).to_have_attribute('data-level', 'high')
    cell.hover()
    playwright.expect(page.locator('#loadMapDetail')).to_contain_text('65.0% 平均 · 峰值 95.0% · 2 个样本')
    cell.click()
    playwright.expect(page.locator('#observationEvidence')).to_be_visible()
    playwright.expect(page.locator('#observationProcesses')).to_contain_text('<script>bad</script>')
    assert page.locator('#observationProcesses script').count() == 0
    assert page.locator('#observationScene option').count() == 2
    playwright.expect(page.locator('#observationEvents')).to_contain_text('同时段重启')
    playwright.expect(page.locator('#observationEvents')).not_to_contain_text('不在所选时段')
    assert page.locator('.load-cell[data-pinned="true"]').count() == 3
    # No one-second history polling, and changing the summary keeps the pinned evidence.
    before = len(calls)
    page.locator('[data-observation-metric="memory_percent"]').click()
    playwright.expect(page.locator('#loadPeak')).to_have_text('—')
    playwright.expect(page.locator('#observationEvidence')).to_be_visible()
    page.wait_for_timeout(1200)
    assert len(calls) == before
    for theme in ('dark', 'light'):
        page.evaluate('(theme) => applyTheme(theme)', theme)
        for width in (1440, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(tmp_path / f'timeline-detail-{theme}-{width}.png'), full_page=True)
    page.locator('.load-cell').nth(0).click()
    playwright.expect(page.locator('#observationProcesses')).to_contain_text('没有保存进程现场')
    playwright.expect(page.locator('#observationEvidenceMetrics')).to_contain_text('无有效采样')
    page.locator('#observationEvidenceClose').click()
    playwright.expect(page.locator('#observationEvidence')).not_to_be_visible()
    page.locator('[data-observation-metric="cpu_percent"]').click()
    page.route('**/performance/server?*', lambda route: route.fulfill(status=503, json={'detail': '读取失败'}))
    page.locator('[data-trend-range="24h"]').click()
    playwright.expect(page.locator('#observationStatus')).to_contain_text('读取失败')
    playwright.expect(page.locator('#loadAverage')).to_have_text('—')
    playwright.expect(page.locator('.load-cell[data-level="empty"]')).to_have_count(180)
    playwright.expect(page.locator('.trend-card-empty')).to_have_count(3)


def test_responsive_workspace_core_controls_themes_and_history_entry(page, http_server, tmp_path):
    preview(page, http_server, system())
    playwright.expect(page.locator('.trend-mini-svg')).to_have_count(3)
    playwright.expect(page.locator('#cpuCorePanel')).not_to_be_visible()
    page.locator('#cpuCoreToggle').click()
    playwright.expect(page.locator('.cpu-core-cell')).to_have_count(8)
    page.locator('[data-cpu-view="detail"]').click()
    playwright.expect(page.locator('#cpuCoreGrid')).to_have_attribute('data-view', 'detail')
    page.locator('#cpuCoreToggle').click()
    page.route('**/performance/server/export?*', lambda route: route.fulfill(
        content_type='text/html', body='<html><body>performance history</body></html>'))
    page.locator('#loadHistoryOpen').click()
    playwright.expect(page.locator('#serverPerformanceDialog')).to_be_visible()
    page.locator('#serverPerformanceClose').click()
    for theme in ('dark', 'light'):
        if theme == 'light':
            page.locator('#themeToggleBtn').click()
        playwright.expect(page.locator('html')).to_have_attribute('data-theme', theme)
        for width in (1440, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1000 if width > 400 else 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('#serverView').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
            assert page.locator('.load-summary-row').evaluate_all(
                'rows => rows.every(el => el.querySelector("span").getBoundingClientRect().right <= el.querySelector("strong").getBoundingClientRect().left)')
            page.screenshot(path=str(tmp_path / f'server-overview-{theme}-{width}.png'), full_page=True)


def test_metric_summary_switches_without_polling_and_container_keeps_multicore_values(page, http_server):
    payload = system()
    now = int(time.time())
    payload['realtime_history'] = [{'ts': now - n, 'cpu_percent': 20, 'memory_percent': 62,
                                  'network_total_kbps': 2048} for n in range(60)]
    calls = []

    def history(route):
        calls.append(route.request.url)
        route.fulfill(json={'interval_seconds': 30, 'points': [
            {'ts': now - 60, 'cpu_percent': 180, 'memory_percent': 40},
            {'ts': now - 30, 'cpu_percent': 220, 'memory_percent': 50}]})

    page.route('**/performance/containers?*', history)
    preview(page, http_server, payload)
    page.locator('[data-observation-metric="memory_percent"]').click()
    playwright.expect(page.locator('#loadAverage')).to_have_text('62.0%')
    playwright.expect(page.locator('#loadAverageLabel')).to_have_text('内存 平均占用')
    page.locator('[data-observation-metric="network_total_kbps"]').click()
    playwright.expect(page.locator('#loadAverage')).to_have_text('2.0 MiB/s')
    assert not calls
    page.locator('#observationTarget').select_option('a' * 64, force=True)
    playwright.expect(page.locator('#loadAverage')).to_have_text('200.0%')
    playwright.expect(page.locator('#loadPeak')).to_have_text('220.0%')
    playwright.expect(page.locator('#loadSampleCount')).to_contain_text('100% = 1 个逻辑核')
    playwright.expect(page.locator('.trend-card')).to_have_count(2)
    playwright.expect(page.locator('.load-cell:visible')).to_have_count(120)
    playwright.expect(page.locator('[data-observation-metric="network_total_kbps"]')).not_to_be_visible()
    # A one-second host refresh must not re-query a 30-second container sample.
    page.wait_for_timeout(2100)
    assert len(calls) == 1
    assert page.locator('.trend-card.cpu').inner_text().count('220%') >= 1
    page.locator('#loadHistoryOpen').click()
    playwright.expect(page.locator('#performanceDialog')).to_be_visible()
    playwright.expect(page.locator('#performanceContainer')).to_contain_text('api-server')
    page.locator('[data-performance-close]').click()
    page.locator('#observationTarget').select_option('', force=True)
    playwright.expect(page.locator('.trend-card')).to_have_count(3)
    playwright.expect(page.locator('#loadAverage')).to_have_text('20.0%')


def test_container_history_does_not_mix_ids_or_fall_back_to_host_on_failure(page, http_server):
    now = int(time.time())
    payload = system()
    page.route('**/performance/containers?*', lambda route: route.fulfill(
        status=503, json={'detail': '数据库暂不可读'}))
    preview(page, http_server, payload)
    page.locator('#observationTarget').select_option('b' * 64, force=True)
    playwright.expect(page.locator('#observationStatus')).to_contain_text('数据库暂不可读')
    playwright.expect(page.locator('#loadAverage')).to_have_text('—')
    playwright.expect(page.locator('.load-cell[data-level="empty"]:visible')).to_have_count(120)
    page.route('**/performance/containers?*', lambda route: route.fulfill(json={
        'interval_seconds': 30, 'points': [{'ts': now - 30, 'cpu_percent': 0, 'memory_percent': 0}]}))
    page.locator('#observationTarget').select_option('c' * 64, force=True)
    playwright.expect(page.locator('#loadAverage')).to_have_text('0.0%')
    # Same name with a new ID remains a separate series; the old history remains selectable.
    payload['docker']['containers'][-1]['id'] = 'd' * 64
    page.locator('#refreshBtn').click()
    playwright.expect(page.locator('#observationTarget')).to_have_value('c' * 64)
    playwright.expect(page.locator('#observationTarget option:checked')).to_contain_text('已离开当前列表')
    assert page.locator('#observationTarget option[value="' + 'd' * 64 + '"]').count() == 1


def test_late_container_reply_is_ignored_and_sampling_gaps_stay_empty(page, http_server, tmp_path):
    now = int(time.time())
    pending = []

    def history(route):
        if 'id=' + 'a' * 64 in route.request.url:
            pending.append(route)
        else:
            route.fulfill(json={'interval_seconds': 30, 'points': [
                {'ts': now - 180, 'cpu_percent': 10, 'memory_percent': 15},
                {'ts': now - 150, 'cpu_percent': 10, 'memory_percent': 15},
                {'ts': now - 30, 'cpu_percent': 20, 'memory_percent': 25}]})

    page.route('**/performance/containers?*', history)
    preview(page, http_server, system())
    page.locator('#observationTarget').select_option('a' * 64, force=True)
    playwright.expect(page.locator('#observationStatus')).to_contain_text('正在读取')
    page.locator('#observationTarget').select_option('b' * 64, force=True)
    playwright.expect(page.locator('#loadAverage')).to_have_text('13.3%')
    assert len(pending) == 1
    pending[0].fulfill(json={'points': [{'ts': now, 'cpu_percent': 999, 'memory_percent': 99}]})
    page.wait_for_timeout(100)
    playwright.expect(page.locator('#loadAverage')).to_have_text('13.3%')
    playwright.expect(page.locator('.trend-card')).to_have_count(2)
    # The 30-minute axis uses 30-second cells and keeps missing observations blank.
    assert page.locator('.trend-card.cpu .trend-point').count() == 3
    assert page.locator('.load-map-row').nth(0).locator('[data-level="empty"]').count() == 57
    for width in (1440, 390, 320):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.locator('#serverView').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        page.screenshot(path=str(tmp_path / f'container-observation-{width}.png'), full_page=True)
    page.locator('#observationTarget').select_option('', force=True)
    page.locator('[data-observation-metric="network_total_kbps"]').click()
    assert page.locator('.load-summary').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
    page.screenshot(path=str(tmp_path / 'network-observation-320.png'), full_page=True)


def test_pinned_interval_ignores_late_events_and_closes_on_target_change(page, http_server):
    now = int(time.time())
    payload = system()
    payload['realtime_history'] = [{'ts': now - offset, 'cpu_percent': 20 + offset}
                                   for offset in (1, 0)]
    preview(page, http_server, payload)
    pending = []
    page.route('**/monitoring', lambda route: pending.append(route))
    page.locator('.load-cell').nth(58).click()
    playwright.expect(page.locator('#observationEvents')).to_contain_text('正在读取')
    page.locator('.load-cell').nth(59).click()
    assert len(pending) == 2
    pending[1].fulfill(json={'events': [{'at_ts': now, 'title': '新时段事件'}]})
    playwright.expect(page.locator('#observationEvents')).to_contain_text('新时段事件')
    pending[0].fulfill(json={'events': [{'at_ts': now, 'title': '过时请求的事件'}]})
    playwright.expect(page.locator('#observationEvents')).not_to_contain_text('过时请求')
    playwright.expect(page.locator('#observationProcesses')).to_contain_text('未关联已保存的进程现场')
    page.route('**/performance/containers?*', lambda route: route.fulfill(json={'points': []}))
    page.locator('#observationTarget').select_option('a' * 64, force=True)
    playwright.expect(page.locator('#observationEvidence')).not_to_be_visible()


def test_missing_saved_scene_and_unreadable_events_keep_resource_evidence(page, http_server):
    now = int(time.time() // 60) * 60
    preview(page, http_server, system(), realtime=False)
    page.route('**/performance/server?*', lambda route: route.fulfill(json={
        'until': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)), 'interval_seconds': 30,
        'points': [{'ts': now - 30, 'cpu_percent': 0, 'cpu_percent_peak': 0,
                    'memory_percent': 0, 'weights': {'cpu_percent': 30, 'memory_percent': 30}}],
        'contexts': [{'at': now - 10, 'ts': now - 30, 'run': 'a' * 32}]}))
    page.route('**/performance/server/context?*', lambda route: route.fulfill(status=404, json={'detail': '现场已过期'}))
    page.route('**/monitoring', lambda route: route.fulfill(status=503, json={'detail': '事件库暂不可读'}))
    page.locator('[data-trend-range="24h"]').click()
    page.locator('[data-trend-range="1h"]').click()
    playwright.expect(page.locator('#loadAverage')).to_have_text('0.0%')
    page.locator('#observationLocate').click()
    playwright.expect(page.locator('#observationEvidenceMetrics')).to_contain_text('0.0%')
    playwright.expect(page.locator('#observationProcesses')).to_contain_text('现场已过期')
    playwright.expect(page.locator('#observationEvents')).to_contain_text('不能据此判断没有异常')
    playwright.expect(page.locator('#loadAverage')).to_have_text('0.0%')


def test_homepage_history_pauses_behind_report_and_resumes_after_close(page, http_server):
    page.clock.install()
    preview(page, http_server, system(), realtime=False)
    calls = []

    def history(route):
        calls.append(route.request.url)
        route.fulfill(json={'points': [{'ts': int(time.time()) - 30, 'cpu_percent': 10}], 'contexts': []})

    page.route('**/performance/server?*', history)
    page.route('**/performance/server/export?*', lambda route: route.fulfill(
        content_type='text/html', body='<html><body>report</body></html>'))
    page.locator('[data-trend-range="24h"]').click()
    page.locator('[data-trend-range="1h"]').click()
    playwright.expect(page.locator('#loadAverage')).to_have_text('10.0%')
    before = len(calls)
    page.locator('#serverPerformanceOpen').click()
    playwright.expect(page.locator('#serverPerformanceDialog')).to_be_visible()
    page.clock.run_for(45000)
    assert len(calls) == before
    with page.expect_response(lambda response: '/performance/server?' in response.url):
        page.locator('#serverPerformanceClose').click()
    playwright.expect(page.locator('#loadAverage')).to_have_text('10.0%')
    assert len(calls) == before + 1
