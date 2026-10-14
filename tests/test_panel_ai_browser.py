import json
import threading

import pytest

import agent
from test_ssh_browser import page as page, playwright
from test_panel_diagnostics import evidence as evidence, manager as manager, model_server as model_server
from test_deployment_removal import http_server as http_server, runtime as runtime
import test_terminal_ai as ai_tests


@pytest.mark.parametrize('width', [1440, 390])
def test_incident_ai_without_ssh_renders_evidence_markdown_and_persisted_history(
        page, http_server, manager, model_server, evidence, tmp_path, width):
    manager.save(ai_tests.config(model_server))
    model_server.gate = threading.Event()
    model_server.content = {'choices': [{'message': {'content': '**CPU 升高需排查**\n\n'
        '| 证据 | 结果 |\n| --- | --- |\n| 当前 CPU | 34% |\n| 历史覆盖 | 部分可用 |\n\n'
        '```sh\ndocker ps\n```\n\n不能确认因果。<script>window.evil=1</script>'}}]}
    page.route('**/monitoring', lambda route: route.fulfill(json={'alerts': [
        {'id': 'alert-test', 'incident_id': 'a1', 'title': 'CPU 持续升高', 'detail': 'CPU 98%', 'source': 'server', 'active': True}],
        'events': [], 'targets': []}))
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(http_server.origin + '/ui?view=events')
    page.locator('#alertList summary').click()
    page.locator('[data-incident-ai="alert-test"]').click()
    playwright.expect(page.locator('#panelAIDialog')).to_be_visible()
    playwright.expect(page.locator('.panel-ai-loading')).to_be_visible()
    playwright.expect(page.locator('.panel-ai-evidence')).to_contain_text('已读取')
    page.screenshot(path=str(tmp_path / f'panel-ai-loading-{width}.png'))
    model_server.gate.set()
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('已保存诊断记录')
    playwright.expect(page.locator('#panelAIMessages strong')).to_have_text('CPU 升高需排查')
    playwright.expect(page.locator('#panelAIMessages table')).to_contain_text('34%')
    assert page.evaluate('window.evil') is None
    assert not page.get_by_role('button', name='填入终端', exact=True).count()
    assert page.locator('#eventsView').evaluate("el => el.classList.contains('active')")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('#panelAIDialog').evaluate('el => el.scrollWidth <= el.clientWidth')
    page.screenshot(path=str(tmp_path / f'panel-ai-diagnosis-{width}.png'))
    page.locator('#panelAIQuestion').fill('继续分析')
    page.locator('#panelAISend').click()
    playwright.expect(page.locator('#panelAIMessages .panel-ai-entry')).to_have_count(2)
    playwright.expect(page.locator('#panelAISend')).to_be_enabled()
    assert len(manager.history.list()['conversations']) == 1
    page.reload()
    page.locator('#alertList summary').click()
    # Keep history access independent of submitting a new model request.
    page.route('**/terminal-ai/settings', lambda route: route.fulfill(json={
        **manager.settings(), 'configured': False, 'secure': True, 'csrf_token': agent._csrf_token(http_server.cookie)}))
    page.locator('[data-incident-ai="alert-test"]').click()
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('模型设置')
    page.locator('#panelAIHistory').click()
    playwright.expect(page.locator('#panelAIHistoryList .tai-history-row')).to_have_count(1)
    page.locator('#panelAIHistoryList .tai-history-row').click()
    playwright.expect(page.locator('#panelAIMessages table')).to_have_count(2)
    playwright.expect(page.locator('#panelAITarget')).to_contain_text('面板主机：')
    page.locator('#panelAIClose').click()


def test_performance_selected_period_and_stop_never_use_ssh(
        page, http_server, manager, model_server, evidence):
    manager.save(ai_tests.config(model_server))
    model_server.gate = threading.Event()
    page.goto(http_server.origin + '/ui?view=server')
    page.locator('#serverPerformanceOpen').click()
    page.locator('[data-server-period="1h"]').click()
    playwright.expect(page.locator('#serverPerformanceFeedback')).to_contain_text('已读取')
    page.locator('#serverPerformanceAI').click()
    playwright.expect(page.locator('#serverPerformanceDialog')).not_to_be_visible()
    playwright.expect(page.locator('#panelAIDialog')).to_be_visible()
    playwright.expect(page.locator('.panel-ai-loading')).to_be_visible()
    playwright.expect(page.locator('.panel-ai-evidence')).to_contain_text('已读取')
    page.wait_for_function('document.querySelector("#panelAITarget").textContent.includes("面板主机：")')
    page.locator('#panelAIStop').click()
    playwright.expect(page.locator('.panel-ai-loading')).not_to_be_visible()
    playwright.expect(page.locator('#panelAISend')).to_be_enabled()
    model_server.gate.set()
    assert '1h' in json.dumps(model_server.calls[0][2])


def test_unconfigured_model_entry_offers_settings_without_sending_or_ssh(
        page, http_server, manager, evidence):
    page.goto(http_server.origin + '/ui?view=server')
    page.evaluate("window.PanelAI.open({kind:'performance', period:'24h'})")
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('模型设置')
    page.locator('#panelAISettings').click()
    playwright.expect(page.locator('#aiSettingsDialog')).to_be_visible()
    assert not manager.jobs
