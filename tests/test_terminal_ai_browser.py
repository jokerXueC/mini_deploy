"""Real panel -> model mock HTTP and SSH -> consent -> diagnosis flows."""
import json

import pytest

import test_terminal_ai as ai_tests
from test_ssh_browser import page as page, fill
from test_ssh_terminal import http_server as http_server, runtime as runtime, manager as manager, peer as peer
ai_manager = ai_tests.manager
model_server = ai_tests.model_server

playwright = pytest.importorskip('playwright.sync_api')


def settings_form(page, http_server, model_server, protocol='openai'):
    page.goto(http_server.origin + '/ui?view=terminal')
    page.get_by_role('button', name='模型设置', exact=True).click()
    if protocol == 'anthropic':
        page.get_by_role('combobox', name='接口类型', exact=True).click()
        page.get_by_role('option', name='Anthropic / Claude 原生', exact=True).click()
    page.locator('#aiBaseUrl').fill(model_server.origin + '/v1')
    page.locator('#aiModel').fill('test-model')
    page.locator('#aiKey').fill('test-key-only')


def test_claude_configuration_and_connection_test_without_ssh(page, http_server, ai_manager, model_server):
    settings_form(page, http_server, model_server, 'anthropic')
    playwright.expect(page.locator('#sshState')).to_have_text('未连接')
    page.get_by_role('button', name='测试连接', exact=True).click()
    playwright.expect(page.locator('#aiSettingsFeedback')).to_contain_text('连接成功', timeout=10000)
    assert model_server.calls[0][0] == '/v1/messages'
    page.get_by_role('button', name='保存设置', exact=True).click()
    playwright.expect(page.locator('#aiSettingsDialog')).not_to_be_visible()
    playwright.expect(page.locator('#aiModelSummary')).to_contain_text('Claude')
    page.reload()
    page.get_by_role('button', name='模型设置', exact=True).click()
    playwright.expect(page.get_by_role('combobox', name='接口类型', exact=True)).to_contain_text('Anthropic')
    playwright.expect(page.locator('#aiKey')).to_have_value('')
    assert ai_manager.settings()['has_key'] and 'test-key-only' not in json.dumps(ai_manager.settings())
    page.set_viewport_size({'width': 390, 'height': 844})
    playwright.expect(page.get_by_role('button', name='保存设置', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_ssh_analysis_requires_preview_and_renders_model_as_text(page, http_server, manager, peer, ai_manager, model_server):
    model_server.content = {'choices': [{'message': {'content': '检查输出 <img src=x onerror=alert(1)>\n```sh\ndocker ps\n```'}}]}
    settings_form(page, http_server, model_server)
    page.get_by_role('button', name='保存设置', exact=True).click()
    playwright.expect(page.locator('#aiSettingsDialog')).not_to_be_visible()
    playwright.expect(page.locator('#sshHost')).to_be_enabled()
    fill(page, peer)
    page.locator('#sshConnect').click()
    page.locator('#sshTrustCheck').check()
    page.locator('#sshTrustConfirm').click()
    playwright.expect(page.locator('#sshState')).to_have_text('已连接')
    playwright.expect(page.locator('.xterm-rows')).to_contain_text('本地测试终端')
    page.locator('#aiAnalyzeOutput').click()
    playwright.expect(page.locator('#aiPreviewDialog')).to_be_visible()
    playwright.expect(page.locator('#aiPreviewSend')).to_be_disabled()
    assert not model_server.calls
    page.locator('#aiConsent').check()
    page.locator('#aiPreviewSend').click()
    playwright.expect(page.locator('#aiMessages')).to_contain_text('docker ps', timeout=10000)
    assert page.locator('#aiMessages img').count() == 0
    assert len(model_server.calls) == 1 and 'safe-password' not in json.dumps(model_server.calls[0][2])
    assert 'PRIVATE KEY' not in ai_manager.path.read_text(encoding='utf-8')
    page.set_viewport_size({'width': 390, 'height': 844})
    playwright.expect(page.locator('#aiPanel')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#aiPanelClose').click()
    page.locator('#sshDisconnect').click()
