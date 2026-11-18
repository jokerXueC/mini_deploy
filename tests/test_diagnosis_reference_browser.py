"""Report references use the existing global chat, draft and persisted history."""
import json

import pytest

import agent
import test_terminal_ai as ai_tests
from browser_navigation import open_global_ai
from test_incident_diagnosis import store as store, complete, KEY, incident
from test_ssh_browser import page as page, playwright
from test_panel_diagnostics import evidence as evidence, manager as manager, model_server as model_server
from test_deployment_removal import http_server as http_server, runtime as runtime
from test_terminal_agent import answer


@pytest.mark.parametrize('width,height,theme', [(1440, 950, 'light'), (1440, 950, 'dark'),
                                              (390, 844, 'light'), (320, 568, 'dark')])
def test_quote_report_preserves_chat_draft_version_and_history(
        page, http_server, manager, model_server, evidence, store, monkeypatch, tmp_path, width, height, theme):
    complete(store)
    store.scan({'events': [{'kind': 'recovery', 'incident_id': KEY, 'at_ts': 1100, 'title': 'CPU 恢复'}]})
    monkeypatch.setattr(agent, '_diagnosis_store', lambda: store)
    manager.save(ai_tests.config(model_server))
    model_server.content = answer('openai')
    page.set_viewport_size({'width': width, 'height': height})
    page.goto(http_server.origin + '/ui?view=events')
    page.evaluate('(theme) => applyTheme(theme)', theme)
    open_global_ai(page)
    page.locator('#panelAIQuestion').fill('先检查服务器')
    page.locator('#panelAISend').click()
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('已保存诊断记录')
    page.locator('#panelAIQuestion').fill('继续核对这份故障报告的原因')
    page.locator('#panelAIMinimize').click()
    page.locator(f'[data-diagnosis="{KEY}"]').click()
    playwright.expect(page.locator('#diagnosisQuote')).to_be_enabled()
    page.locator('#diagnosisVersion').select_option('1', force=True)
    playwright.expect(page.locator('#diagnosisQuote')).to_be_enabled()
    playwright.expect(page.locator('#diagnosisDownload')).to_have_attribute('href', f'incident-diagnosis/report?id={KEY}&revision=1&download=md')
    page.locator('#diagnosisQuote').click()
    playwright.expect(page.locator('#diagnosisDialog')).not_to_be_visible()
    playwright.expect(page.locator('#panelAIDialog')).to_be_visible()
    playwright.expect(page.locator('#panelAIReference')).to_contain_text('CPU 持续高占用')
    playwright.expect(page.locator('#panelAIReference small')).to_have_text('v1')
    playwright.expect(page.locator('#panelAIQuestion')).to_have_value('继续核对这份故障报告的原因')
    playwright.expect(page.locator('.panel-ai-question')).to_have_text('先检查服务器')
    assert len(model_server.calls) == 1  # Attaching alone must never call a model or execute suggested commands.
    page.locator('#panelAIReference .panel-ai-reference-open').click()
    playwright.expect(page.locator('#diagnosisQuote')).to_be_enabled()
    assert page.locator('#diagnosisVersion').input_value() == '1'
    page.locator('#diagnosisClose').click()
    playwright.expect(page.locator('#panelAIQuestion')).to_have_value('继续核对这份故障报告的原因')
    page.screenshot(path=str(tmp_path / f'report-reference-{width}-{theme}.png'))
    assert page.locator('#panelAIDialog').evaluate('e => e.scrollWidth <= e.clientWidth')
    assert page.locator('#panelAIReference').evaluate('e => e.scrollWidth <= e.clientWidth')
    assert page.locator('#panelAISend').bounding_box()['y'] + page.locator('#panelAISend').bounding_box()['height'] <= height
    assert page.locator('#panelAIReference img').first.evaluate('(e) => e.complete && e.naturalWidth > 0')
    page.locator('#panelAISend').click()
    playwright.expect(page.locator('#panelAISend')).to_be_enabled()
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('已保存诊断记录')
    playwright.expect(page.locator('#panelAIMessages .panel-ai-reference')).to_have_count(1)
    assert len(model_server.calls) == 2
    assert KEY in json.dumps(model_server.calls[-1][2])
    page.locator('#panelAIQuestion').fill('保留未发送问题')
    page.locator('#panelAIReference').get_by_role('button', name='移除本次报告引用').click()
    playwright.expect(page.locator('#panelAIReference')).not_to_be_visible()
    playwright.expect(page.locator('#panelAIQuestion')).to_have_value('保留未发送问题')
    playwright.expect(page.locator('#panelAIMessages .panel-ai-reference')).to_have_count(1)
    page.reload()
    open_global_ai(page)
    page.locator('#panelAIHistory').click()
    page.locator('#panelAIHistoryList .tai-history-row').first.click()
    playwright.expect(page.locator('#panelAIReference small')).to_have_text('v1')
    playwright.expect(page.locator('.panel-ai-question')).to_have_count(2)
    page.locator('#panelAIQuestion').fill('恢复对话后继续复核')
    page.locator('#panelAISend').click()
    playwright.expect(page.locator('#panelAISend')).to_be_enabled()
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('已保存诊断记录')
    assert len(model_server.calls) == 3
    records = manager.history.list()['conversations']
    assert len(records) == 1
    turns = manager.history.detail(records[0]['id'])['turns']
    assert len(turns) == 3 and turns[-1]['reference']['revision'] == 1


def test_pending_report_cannot_be_quoted_and_failed_load_does_not_reuse_old_reference(
        page, http_server, manager, model_server, evidence, store, monkeypatch):
    complete(store)
    pending = 'b' * 24
    store.scan({'alerts': [incident(incident_id=pending)]})
    monkeypatch.setattr(agent, '_diagnosis_store', lambda: store)
    page.goto(http_server.origin + '/ui?view=events')
    page.locator(f'[data-diagnosis="{pending}"]').click()
    playwright.expect(page.locator('#diagnosisReportStatus')).to_contain_text('尚未生成报告')
    playwright.expect(page.locator('#diagnosisQuote')).to_be_disabled()
    page.locator('#diagnosisClose').click()
    page.locator(f'[data-diagnosis="{KEY}"]').click()
    playwright.expect(page.locator('#diagnosisQuote')).to_be_enabled()
    page.evaluate(f"IncidentDiagnosis.open('{KEY}', 99)")
    playwright.expect(page.locator('#diagnosisQuote')).to_be_disabled()
    assert not model_server.calls


def test_quoting_during_active_global_task_keeps_job_and_next_turn_draft(
        page, http_server, manager, model_server, evidence, store, monkeypatch):
    complete(store)
    monkeypatch.setattr(agent, '_diagnosis_store', lambda: store)
    manager.save(ai_tests.config(model_server))
    model_server.content = answer('openai')
    page.goto(http_server.origin + '/ui?view=events')
    open_global_ai(page)
    page.evaluate('''() => {
      const original = window.fetch, gate = new Promise(resolve => window.releaseJobPoll = resolve);
      window.fetch = async (...args) => {
        if (String(args[0]).startsWith('/terminal-ai/jobs?')) await gate;
        return original(...args);
      };
    }''')
    page.locator('#panelAIQuestion').fill('正在检查服务器')
    page.locator('#panelAISend').click()
    playwright.expect(page.locator('#panelAIQuestion')).to_have_value('')
    page.locator('#panelAIQuestion').fill('我的下一个问题')
    page.locator('#panelAIMinimize').click()
    page.locator(f'[data-diagnosis="{KEY}"]').click()
    playwright.expect(page.locator('#diagnosisQuote')).to_be_enabled()
    page.locator('#diagnosisQuote').click()
    playwright.expect(page.locator('#panelAIStop')).to_be_visible()
    playwright.expect(page.locator('#panelAIQuestion')).to_have_value('我的下一个问题')
    playwright.expect(page.locator('#panelAIReference')).to_be_visible()
    page.evaluate('window.releaseJobPoll()')
    playwright.expect(page.locator('#panelAIFeedback')).to_contain_text('已保存诊断记录')
    assert len(model_server.calls) == 1
    turns = manager.history.detail(manager.history.list()['conversations'][0]['id'])['turns']
    assert turns[0]['reference'] == {}  # Reference belongs to the next turn, not the running task.
