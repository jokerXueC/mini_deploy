import time

import pytest

import agent
from server_performance import History
from test_ssh_browser import page as page, playwright
from test_deployment_removal import runtime as runtime, http_server as http_server
from test_server_performance import sample


@pytest.mark.parametrize('width', [1440, 390])
def test_resource_configuration_and_persisted_incident_ui(page, http_server, runtime, monkeypatch, tmp_path, width):
    page.unroute('**/monitoring')
    page.set_viewport_size({'width': width, 'height': 950})
    now = time.time()
    history = History(runtime)
    history.observe(sample(95), now - 10)
    history.record_context({'at': now - 10, 'available': True, 'warnings': [], 'processes': [
        {'pid': 88, 'name': '<script>bad</script>', 'cpu_percent': 420, 'rss_mib': 850,
         'read_kib_s': 8, 'write_kib_s': 4, 'service': 'api.service', 'container': ''}]}, sample(95))
    history.flush(now, force=True)
    monkeypatch.setattr(agent, '_server_performance', lambda: history)
    monkeypatch.setattr(agent, '_operations_inventory', lambda *_: {'hostname': 'demo', 'processes': [], 'ports': [], 'sources': [{'id': 'service:api.service', 'kind': 'service', 'name': 'api'}]})
    monitor = agent._monitor()
    monitor.observe('server:cpu', True, 'CPU 使用率过高', '95%', 'server', immediate=True)
    monitor.record_event('lifecycle', 'api 容器发生重启', '', 'docker_events')
    page.goto(http_server.origin + '/ui?view=events')
    page.locator('.monitor-settings > summary').click()
    page.locator('.resource-rule-settings > summary').click()
    page.locator('#monitorCpuThreshold').fill('92')
    page.locator('#monitorResourceRecovery').fill('35')
    page.locator('#monitorSettingsForm button[type=submit]').click()
    playwright.expect(page.locator('#monitorFeedback')).to_contain_text('提醒规则已保存')
    assert monitor.settings['cpu_threshold'] == 92 and monitor.settings['resource_recovery_seconds'] == 35
    assert page.locator('#prometheusPanel, #incidentOperations').count() == 0
    playwright.expect(page.locator('#eventTimeline')).not_to_contain_text('api 容器发生重启')
    page.screenshot(path=str(tmp_path / f'monitoring-closure-settings-{width}.png'), full_page=True)
    page.locator('#alertList summary').first.click()
    page.locator('[data-incident-diagnose]').first.click()
    playwright.expect(page.locator('#opsIncident')).to_contain_text('已保存的进程现场')
    playwright.expect(page.locator('#opsIncident')).to_contain_text('<script>bad</script>')
    playwright.expect(page.locator('#opsTimeline')).to_contain_text('api 容器发生重启')
    assert page.evaluate('window.bad') is None
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'monitoring-closure-incident-{width}.png'), full_page=True)
