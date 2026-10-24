import json
import threading

import pytest

import agent
from test_ssh_browser import page as page, playwright
from test_panel_diagnostics import evidence as evidence, manager as manager, model_server as model_server
from test_panel_diagnostics import code_projects as code_projects
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


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_minimized_diagnosis_continues_across_views_and_restores_draft_and_result(
        page, http_server, manager, model_server, evidence, tmp_path, width, theme):
    manager.save(ai_tests.config(model_server))
    model_server.gate = threading.Event()
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(http_server.origin + '/ui?view=server')
    page.evaluate('(theme) => applyTheme(theme)', theme)
    page.evaluate("window.PanelAI.open({kind:'overview'})")
    playwright.expect(page.locator('.panel-ai-loading')).to_be_visible()
    page.wait_for_function('document.querySelector("#panelAITarget").textContent.includes("面板主机：")')
    page.locator('#panelAIQuestion').fill('保留这个追问草稿')
    page.locator('#panelAIMinimize').click()
    dock = page.locator('#panelAIDock')
    playwright.expect(page.locator('#panelAIDialog')).not_to_be_visible()
    playwright.expect(dock).to_be_visible()
    playwright.expect(dock).to_be_focused()
    playwright.expect(page.locator('#panelAIDockStatus')).to_have_text('正在分析')
    assert dock.bounding_box()['x'] == 0
    page.locator('#eventsViewTab').click()
    playwright.expect(page.locator('#eventsView')).to_have_class('dashboard-view active')
    # Another AI entry restores the active task instead of cancelling/replacing it.
    page.evaluate("window.PanelAI.open({kind:'incident', id:'another-incident'})")
    playwright.expect(page.locator('#panelAIDialog')).to_be_visible()
    playwright.expect(page.locator('#panelAIQuestion')).to_have_value('保留这个追问草稿')
    assert len(manager.jobs) == 1
    page.locator('#panelAIMinimize').click()
    model_server.gate.set()
    playwright.expect(page.locator('#panelAIDockStatus')).to_have_text('已完成', timeout=15000)
    playwright.expect(dock).to_have_attribute('data-state', 'done')
    assert len(model_server.calls) == 1
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'panel-ai-docked-{width}-{theme}.png'))
    dock.click()
    playwright.expect(page.locator('#panelAIDialog')).to_be_visible()
    playwright.expect(dock).not_to_be_visible()
    playwright.expect(page.locator('#panelAIQuestion')).to_have_value('保留这个追问草稿')
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('已保存诊断记录')
    assert page.locator('#panelAIMessages .panel-ai-entry').count() == 1
    assert len(manager.history.list()['conversations']) == 1
    page.locator('#panelAIClose').click()
    playwright.expect(dock).not_to_be_visible()


def test_minimized_preparation_failure_restore_and_stop(
        page, http_server, manager, model_server, evidence):
    manager.save(ai_tests.config(model_server))
    model_server.gate = threading.Event()
    page.goto(http_server.origin + '/ui?view=server')
    # Hold setup so minimizing before submission also keeps the session alive.
    page.evaluate('''() => {
      const original = window.fetch;
      window.panelAISetupGate = new Promise(resolve => window.releasePanelAISetup = resolve);
      window.fetch = async (...args) => {
        if (String(args[0]) === '/terminal-ai/settings') await window.panelAISetupGate;
        return original(...args);
      };
      window.PanelAI.open({kind:'overview'});
    }''')
    page.locator('#panelAIMinimize').click()
    playwright.expect(page.locator('#panelAIDockStatus')).to_have_text('准备中')
    page.evaluate('window.releasePanelAISetup()')
    playwright.expect(page.locator('#panelAIDockStatus')).to_have_text('正在分析')
    page.route('**/terminal-ai/jobs?*', lambda route: route.fulfill(status=503, json={'detail': '测试诊断连接中断'}))
    playwright.expect(page.locator('#panelAIDockStatus')).to_have_text('分析失败')
    page.locator('#panelAIDock').click()
    playwright.expect(page.locator('#panelAIFeedback')).to_have_text('测试诊断连接中断')
    page.unroute('**/terminal-ai/jobs?*')
    model_server.gate.set()
    page.locator('#panelAIClose').click()
    page.evaluate("window.PanelAI.open({kind:'overview'})")
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('已保存诊断记录')
    model_server.gate = threading.Event()
    page.locator('#panelAIQuestion').fill('再检查一次')
    page.locator('#panelAISend').click()
    playwright.expect(page.locator('#panelAIStop')).to_be_visible()
    page.locator('#panelAIStop').click()
    page.locator('#panelAIMinimize').click()
    playwright.expect(page.locator('#panelAIDockStatus')).to_have_text('已停止')
    page.locator('#panelAIDock').click()
    playwright.expect(page.locator('#panelAISend')).to_be_enabled()
    page.locator('#panelAIClose').click()
    playwright.expect(page.locator('#panelAIDock')).not_to_be_visible()
    model_server.gate.set()


@pytest.mark.parametrize('width', [1440, 390])
def test_code_scope_selection_starts_separate_conversations_and_restores_scope(
        page, http_server, manager, model_server, evidence, code_projects, tmp_path, width):
    manager.save(ai_tests.config(model_server))
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(http_server.origin + '/ui?view=server')
    page.evaluate("window.PanelAI.open({kind:'overview'})")
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('已保存诊断记录')
    page.get_by_role('combobox', name='代码范围').click()
    page.get_by_role('option', name='aimore · /opt/aimore', exact=True).click()
    playwright.expect(page.locator('#panelAIMessages .panel-ai-entry')).to_have_count(0)
    playwright.expect(page.locator('#panelAICodeStatus')).to_have_text('/opt/aimore · 只读')
    page.locator('#panelAIQuestion').fill('检查 aimore 的 claim 接口代码，不修改')
    page.locator('#panelAISend').click()
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('已保存诊断记录')
    playwright.expect(page.locator('#panelAITarget')).to_contain_text('代码范围：/opt/aimore')
    scoped = next(c for c in manager.history.list()['conversations'] if '/opt/aimore' in c['target'])
    for theme in ('dark', 'light'):
        page.evaluate('(theme) => applyTheme(theme)', theme)
        assert page.locator('#panelAIDialog').evaluate('el => el.scrollWidth <= el.clientWidth')
        page.screenshot(path=str(tmp_path / f'panel-code-scope-{theme}-{width}.png'))
    page.get_by_role('combobox', name='代码范围').click()
    page.get_by_role('option', name='other · /srv/other', exact=True).click()
    page.locator('#panelAIQuestion').fill('检查这个项目')
    page.locator('#panelAISend').click()
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('已保存诊断记录')
    assert len(manager.history.list()['conversations']) == 3
    # The queued close event must not cancel an immediately restored dialog.
    page.evaluate('''(id) => {
        document.querySelector('#panelAIDialog').close();
        return window.PanelAI.restore(id);
    }''', scoped['id'])
    playwright.expect(page.locator('#panelAICodeProject')).to_have_value('a' * 32)
    playwright.expect(page.locator('#panelAICodeStatus')).to_contain_text('/opt/aimore')
    playwright.expect(page.locator('#panelAIMessages .panel-ai-entry')).to_have_count(1)
    code_projects.projects.pop(0)
    page.evaluate('''(id) => {
        document.querySelector('#panelAIDialog').close();
        return window.PanelAI.restore(id);
    }''', scoped['id'])
    playwright.expect(page.locator('#panelAISend')).to_be_disabled()
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('历史代码目录已不可用')
    page.get_by_role('combobox', name='代码范围').click()
    page.get_by_role('option', name='仅运维证据', exact=True).click()
    playwright.expect(page.locator('#panelAIMessages .panel-ai-entry')).to_have_count(0)
    playwright.expect(page.locator('#panelAISend')).to_be_enabled()
