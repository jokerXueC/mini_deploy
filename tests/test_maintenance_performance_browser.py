import time
import re

import pytest

from scripts.preview_operations import examples
from test_monitoring_browser import dashboard as dashboard, playwright


@pytest.fixture
def preview(dashboard):
    page, state, origin = dashboard
    _, evidence = examples()
    server = evidence['server_now']
    page.route('**/ssh', lambda r: r.fulfill(json={'enabled': False, 'profiles': [], 'sessions': []}))
    container = {'id': 'a' * 64, 'name': 'api', 'state': 'running', 'cpu_percent': 138, 'memory_percent': 50}
    page.route('**/status', lambda r: r.fulfill(json={'agent': {'status': 'ok'}, 'csrf_token': 'test-token',
        'system': {'server': server, 'docker': {'available': True, 'containers': [container]}}}))
    page.route('**/system-metrics', lambda r: r.fulfill(json={'server': server}))
    page.route('**/system/swap', lambda r: r.fulfill(json={
        'supported': False, 'editable': False, 'detail': '本地预览不操作 Swap', 'job': {}}))
    control = {'calls': [], 'running': False, 'finished': False, 'offline': False}
    def api(route):
        control['calls'].append((route.request.method, route.request.url, route.request.post_data_json))
        if route.request.method == 'POST':
            assert route.request.headers['x-csrf-token'] == 'test-token'
            if route.request.url.endswith('/check'):
                result = {'revision': 'b' * 40, 'channel': 'gitee'}
            else:
                assert route.request.post_data_json['confirm']
                control['running'] = True
                result = {'job': 'c' * 32}
        else:
            if control['offline']:
                route.abort('connectionrefused')
                return
            result = {'supported': True, 'version': {'revision': 'a' * 40}, 'csrf_token': 'test-token',
                'notice': '只恢复程序，当前配置与数据保留', 'backup_directory': '/var/backups/mini-deploy-agent',
                'backups': [{'kind': 'program', 'id': 'program-test', 'at': time.time()}],
                'jobs': ([{'state': 'succeeded' if control['finished'] else 'running', 'phase': '更新完成' if control['finished'] else '健康检查', 'started_at': time.time()}] if control['running'] else [])}
        route.fulfill(json=result)
    page.route(re.compile(r'/panel-maintenance(?:/check)?$'), api)
    page.route('**/performance/server?*', lambda r: r.fulfill(json={'points': [], 'incidents': [], 'interval_seconds': 30}))
    page.route('**/performance/containers?*', lambda r: r.fulfill(json={'points': [
        {'ts': time.time() - 600 + n * 30, 'cpu_percent': 150 + n, 'memory_percent': 20 + n / 2} for n in range(21)],
        'interval_seconds': 30, 'retention_days': 7, 'notice': '缺失采样不补零'}))
    page.goto(origin + '/ui?view=server')
    yield page, state, control
    assert not state['errors']


@pytest.mark.parametrize('width', [1440, 390])
def test_performance_details_and_container_history_responsive(preview, tmp_path, width):
    page, _, _ = preview
    page.set_viewport_size({'width': width, 'height': 900})
    page.locator('#performancePanel > summary').click()
    playwright.expect(page.locator('#performanceValues')).to_contain_text('vda')
    playwright.expect(page.locator('#performanceValues')).to_contain_text('12.5%')
    page.locator('[data-performance-container]').click()
    playwright.expect(page.locator('#performanceCharts svg')).to_have_count(2)
    playwright.expect(page.locator('#performanceNotice')).to_contain_text('缺失采样')
    assert page.locator('#performanceCharts path').first.get_attribute('d').startswith('M')
    bounds = page.locator('#performanceDialog').bounding_box()
    assert bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= 901
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'performance-{width}.png'), full_page=True)
    page.locator('[data-performance-period="7d"]').click()
    playwright.expect(page.locator('[data-performance-period="7d"]')).to_have_attribute('aria-selected', 'true')


@pytest.mark.parametrize('width', [1440, 390])
def test_maintenance_confirmation_polling_and_reconnect(preview, tmp_path, width):
    page, _, control = preview
    page.set_viewport_size({'width': width, 'height': 900})
    page.locator('#panelMaintenanceOpen').click()
    playwright.expect(page.locator('#maintenanceVersion')).to_contain_text('aaaaaaaa')
    playwright.expect(page.locator('#maintenanceUpdate')).to_be_disabled()
    page.locator('#maintenanceCheck').click()
    playwright.expect(page.locator('#maintenanceUpdate')).to_be_enabled()
    page.once('dialog', lambda dialog: dialog.dismiss())
    page.locator('#maintenanceUpdate').click()
    assert not control['running']
    page.screenshot(path=str(tmp_path / f'maintenance-{width}.png'), full_page=True)
    page.clock.install()
    page.once('dialog', lambda dialog: dialog.accept())
    page.locator('#maintenanceUpdate').click()
    playwright.expect(page.locator('#maintenanceJob')).to_contain_text('健康检查')
    playwright.expect(page.locator('#maintenanceBackup')).to_be_disabled()
    control['offline'] = True
    page.clock.run_for(2600)
    playwright.expect(page.locator('#maintenanceFeedback')).to_contain_text('等待重连')
    control.update(offline=False, finished=True)
    page.clock.run_for(5100)
    playwright.expect(page.locator('#maintenanceJob')).to_contain_text('更新完成')
    playwright.expect(page.locator('#maintenanceBackup')).to_be_enabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.mouse.click(1, 1)
    playwright.expect(page.locator('#maintenanceDialog')).to_be_visible()
    page.keyboard.press('Escape')
    playwright.expect(page.locator('#maintenanceDialog')).not_to_be_visible()
