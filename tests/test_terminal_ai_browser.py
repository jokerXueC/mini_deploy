"""Real panel -> model mock HTTP and optional SSH -> conversational diagnosis."""
import json
import hashlib
import threading

import pytest

import agent
import test_terminal_ai as ai_tests
import test_terminal_agent as agent_tests
from test_ssh_browser import page as page, fill
from test_ssh_terminal import http_server as http_server, runtime as runtime, manager as manager, peer as peer
from ssh_sftp_peer import enable_sftp
ai_manager = ai_tests.manager
model_server = ai_tests.model_server

playwright = pytest.importorskip('playwright.sync_api')


def test_chat_waiting_animation_and_saved_history_after_refresh(page, http_server, ai_manager, model_server, tmp_path):
    ai_manager.save(ai_tests.config(model_server))
    model_server.gate = threading.Event()
    page.goto(http_server.origin + '/ui?view=terminal')
    page.locator('#sshConnectionToggle').click()
    assert page.locator('#sshLayout').evaluate("el => el.classList.contains('connections-collapsed')")
    page.locator('#aiChatOpen').click()
    page.locator('#aiQuestion').fill('检查这台服务器的状态')
    page.locator('#aiAskSend').click()
    playwright.expect(page.locator('.tai-loading')).to_be_visible()
    assert page.locator('.tai-loading .tai-icon').evaluate("el => getComputedStyle(el).animationName") == 'tai-spin'
    page.screenshot(path=str(tmp_path / 'chat-waiting-desktop.png'), full_page=True)
    model_server.gate.set()
    playwright.expect(page.locator('#aiFeedback')).to_have_text('可继续追问')
    playwright.expect(page.locator('.tai-loading')).not_to_be_visible()
    page.reload()
    page.locator('#aiChatOpen').click()
    page.locator('#aiHistoryOpen').click()
    playwright.expect(page.locator('#aiHistoryDialog')).to_be_visible()
    playwright.expect(page.locator('.tai-history-row')).to_contain_text('检查这台服务器的状态')
    page.screenshot(path=str(tmp_path / 'chat-history-desktop.png'), full_page=True)
    page.locator('.tai-history-row').click()
    playwright.expect(page.locator('#aiHistoryBar')).to_be_visible()
    playwright.expect(page.locator('#aiMessages')).to_contain_text('检查容器网络')
    playwright.expect(page.locator('.tai-loading')).not_to_be_visible()
    page.locator('#aiQuestion').fill('继续分析')
    page.locator('#aiAskSend').click()
    playwright.expect(page.locator('#aiFeedback')).to_have_text('可继续追问')
    assert len(model_server.calls) == 2
    assert '检查容器网络' in json.dumps(model_server.calls[-1][2], ensure_ascii=False)
    assert len(ai_manager.history.list()['conversations']) == 1
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'chat-history-mobile.png'))
    page.locator('#aiHistoryOpen').click()
    playwright.expect(page.locator('#aiHistoryDialog')).to_be_visible()
    page.screenshot(path=str(tmp_path / 'chat-history-mobile-list.png'))
    page.get_by_role('button', name='关闭对话历史', exact=True).click()


def settings_form(page, http_server, model_server, protocol='openai'):
    page.goto(http_server.origin + '/ui?view=terminal')
    page.get_by_role('button', name='模型设置', exact=True).click()
    if protocol == 'anthropic':
        page.get_by_role('combobox', name='接口类型', exact=True).click()
        page.get_by_role('option', name='Anthropic / Claude 原生', exact=True).click()
    page.locator('#aiBaseUrl').fill(model_server.origin + '/v1')
    page.locator('#aiModel').fill('test-model')
    page.locator('#aiKey').fill('test-key-only')


def test_file_repair_read_approval_backup_validate_reload_workflow(
        page, http_server, manager, peer, ai_manager, model_server, tmp_path):
    root = tmp_path / 'remote'
    root.mkdir()
    path = root / 'app.conf'
    path.write_text('port=8080\n', encoding='utf-8')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sequence = [
        ('search_files', {'path': '/', 'pattern': '*.conf'}),
        ('read_text_file', {'path': '/app.conf'}),
        ('inspect_file', {'path': '/app.conf'}),
        ('edit_file', {'path': '/app.conf', 'old_text': 'port=8080', 'new_text': 'port=8081', 'expected_sha256': digest}),
        ('validate_config', {'runtime': 'nginx', 'path': '/app.conf'}),
        ('reload_service', {'service': 'nginx'}),
        ('get_service_status', {'service': 'nginx'}),
    ]
    model_server.content = lambda body, n: (agent_tests.tool_response('openai', *sequence[n - 1], f'call_{n}')
                                           if n <= len(sequence) else agent_tests.answer('openai'))
    ai_manager.save(ai_tests.config(model_server))
    page.goto(http_server.origin + '/ui?view=terminal')
    fill(page, peer)
    page.locator('#sshConnect').click()
    page.locator('#sshTrustCheck').check()
    page.locator('#sshTrustConfirm').click()
    playwright.expect(page.locator('#sshState')).to_have_text('已连接')
    enable_sftp(peer, root)
    page.locator('#aiChatOpen').click()
    page.locator('#aiQuestion').fill('查找 app.conf，把端口改成 8081，校验后重载 nginx 并检查状态')
    page.locator('#aiAskSend').click()
    playwright.expect(page.locator('.tai-approval')).to_be_visible(timeout=10000)
    playwright.expect(page.locator('.tai-approval')).to_contain_text('原片段：')
    playwright.expect(page.locator('.tai-approval')).to_contain_text('port=8081')
    assert path.read_text() == 'port=8080\n' and not list(root.glob('*.mini-deploy-backup.*'))
    page.screenshot(path=str(tmp_path / 'file-edit-approval-desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'file-edit-approval-mobile.png'))
    page.get_by_role('button', name='同意执行', exact=True).click()
    playwright.expect(page.locator('.tai-approval')).to_contain_text('systemctl --no-ask-password reload', timeout=10000)
    assert path.read_text() == 'port=8081\n'
    assert next(root.glob('*.mini-deploy-backup.*')).read_text() == 'port=8080\n'
    assert len(peer['exec']) == 1 and b'nginx -t' in peer['exec'][0]
    page.get_by_role('button', name='同意执行', exact=True).click()
    playwright.expect(page.locator('#aiFeedback')).to_have_text('可继续追问', timeout=10000)
    assert len(peer['exec']) == 3 and len(model_server.calls) == 8
    assert digest in json.dumps(model_server.calls[3][2])
    assert 'port=8080' in json.dumps(model_server.calls[2][2])
    history_id = ai_manager.history.list()['conversations'][0]['id']
    assert 'mini-deploy-backup.' in json.dumps(ai_manager.history.detail(history_id))
    page.locator('#aiPanelClose').click()
    page.locator('#sshDisconnect').click()


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


def test_ssh_chat_opens_without_sending_then_followup_and_optional_output(page, http_server, manager, peer, ai_manager, model_server, tmp_path):
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
    page.locator('#aiChatOpen').click()
    playwright.expect(page.locator('#aiPanel')).to_be_visible()
    playwright.expect(page.locator('#aiQuestion')).to_be_focused()
    assert not model_server.calls
    assert page.locator('#aiPreviewDialog').count() == 0
    page.locator('#aiQuestion').fill('分析当前终端情况')
    page.locator('#aiAskSend').click()
    playwright.expect(page.locator('#aiMessages')).to_contain_text('docker ps', timeout=10000)
    playwright.expect(page.locator('#aiFeedback')).to_have_text('可继续追问')
    assert page.locator('#aiMessages img').count() == 0
    assert len(model_server.calls) == 1 and 'safe-password' not in json.dumps(model_server.calls[0][2])
    assert '本地测试终端' in json.dumps(model_server.calls[0][2], ensure_ascii=False)
    page.locator('#aiQuestion').fill('为什么建议 docker ps？')
    page.locator('#aiQuestion').press('Enter')
    playwright.expect(page.locator('#aiMessages .tai-entry')).to_have_count(2)
    playwright.expect(page.locator('#aiFeedback')).to_have_text('可继续追问')
    assert [m['role'] for m in model_server.calls[-1][2]['messages']] == ['system', 'user', 'assistant', 'user']
    page.screenshot(path=str(tmp_path / 'chat-desktop.png'), full_page=True)
    page.locator('#aiNewChat').click()
    playwright.expect(page.locator('#aiEmpty')).to_be_visible()
    page.locator('#aiShareOutput').uncheck()
    page.locator('#aiQuestion').fill('什么是容器网络？')
    page.locator('#aiAskSend').click()
    playwright.expect(page.locator('#aiFeedback')).to_have_text('可继续追问')
    messages = model_server.calls[-1][2]['messages']
    assert len(messages) == 2 and '本地测试终端' not in json.dumps(messages, ensure_ascii=False)
    assert 'PRIVATE KEY' not in ai_manager.path.read_text(encoding='utf-8')
    page.set_viewport_size({'width': 390, 'height': 844})
    playwright.expect(page.locator('#aiPanel')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'chat-mobile.png'), full_page=True)
    page.locator('#aiQuestion').fill('这是下一条问题\n继续补充说明')
    assert len(model_server.calls) == 3
    page.screenshot(path=str(tmp_path / 'chat-mobile-viewport.png'))
    page.locator('#aiPanelClose').click()
    page.locator('#sshDisconnect').click()


def test_general_chat_does_not_require_ssh_and_preserves_unsent_question(page, http_server, ai_manager, model_server):
    page.goto(http_server.origin + '/ui?view=terminal')
    page.locator('#aiChatOpen').click()
    playwright.expect(page.locator('#aiEmpty')).to_be_visible()
    page.locator('#aiQuestion').fill('Docker 的网络怎么用？')
    page.locator('#aiAskSend').click()
    playwright.expect(page.locator('#aiSettingsDialog')).to_be_visible()
    playwright.expect(page.locator('#aiQuestion')).to_have_value('Docker 的网络怎么用？')
    assert not model_server.calls
    page.locator('#aiBaseUrl').fill(model_server.origin + '/v1')
    page.locator('#aiModel').fill('test-model')
    page.locator('#aiKey').fill('test-key-only')
    page.locator('#aiSave').click()
    playwright.expect(page.locator('#aiSettingsDialog')).not_to_be_visible()
    page.locator('#aiAskSend').click()
    playwright.expect(page.locator('#aiFeedback')).to_have_text('可继续追问')
    playwright.expect(page.locator('#sshState')).to_have_text('未连接')
    playwright.expect(page.locator('#aiTargetLabel')).to_have_text('普通对话')
    playwright.expect(page.locator('#aiShareOutput')).to_be_disabled()
    assert len(model_server.calls) == 1 and len(model_server.calls[0][2]['messages']) == 2


@pytest.mark.parametrize('protocol,approve', [('openai', True), ('anthropic', False)])
def test_agent_permission_dialog_progress_and_actual_operation_approval(page, http_server, manager, peer, ai_manager, model_server, tmp_path, protocol, approve):
    model_server.content = lambda body, n: (
        agent_tests.tool_response(protocol, 'list_containers', {}) if n == 1 else
        agent_tests.tool_response(protocol, 'restart_service', {'service': 'nginx'}, 'call_two') if n == 2 else
        agent_tests.answer(protocol))
    settings_form(page, http_server, model_server, protocol)
    page.locator('#aiSave').click()
    playwright.expect(page.locator('#aiSettingsDialog')).not_to_be_visible()
    playwright.expect(page.locator('#sshHost')).to_be_enabled()
    fill(page, peer)
    page.locator('#sshConnect').click()
    playwright.expect(page.locator('#sshFingerprintDialog')).to_be_visible(timeout=10000)
    page.locator('#sshTrustCheck').check()
    page.locator('#sshTrustConfirm').click()
    playwright.expect(page.locator('#sshState')).to_have_text('已连接')
    page.locator('#aiChatOpen').click()
    permission = page.get_by_role('combobox', name='操作权限', exact=True)
    playwright.expect(permission).to_be_enabled()
    permission.click()
    page.get_by_role('option', name='自动操作', exact=True).click()
    playwright.expect(page.locator('#aiAccessDialog')).to_be_visible()
    playwright.expect(page.locator('#aiAccessConfirm')).to_be_disabled()
    assert all(p['mode'] != 'auto' for p in ai_manager.permissions.values())
    page.get_by_role('button', name='取消 AI 授权', exact=True).click()
    playwright.expect(permission).to_contain_text('修改需确认')
    permission.click()
    page.get_by_role('option', name='只读检查', exact=True).click()
    playwright.expect(page.locator('#aiPermission')).to_have_value('readonly')
    playwright.expect(permission).to_be_enabled()
    permission.click()
    page.get_by_role('option', name='修改需确认', exact=True).click()
    playwright.expect(page.locator('#aiPermission')).to_have_value('approve')
    playwright.expect(page.locator('#aiAccessDialog')).not_to_be_visible()
    playwright.expect(permission).to_contain_text('修改需确认')
    page.locator('#aiQuestion').fill('检查容器状态，必要时重启 nginx')
    page.locator('#aiAskSend').click()
    playwright.expect(page.locator('.tai-approval')).to_be_visible(timeout=10000)
    playwright.expect(page.locator('.tai-steps')).to_contain_text('已完成')
    playwright.expect(page.locator('.tai-approval')).to_contain_text('nginx.service')
    assert len(peer['exec']) == 1 and b'docker ps' in peer['exec'][0]
    page.screenshot(path=str(tmp_path / 'agent-approval-desktop.png'), full_page=True)
    page.locator('#themeToggleBtn').click()
    page.set_viewport_size({'width': 390, 'height': 844})
    page.wait_for_function("!document.documentElement.matches('.theme-reveal-light, .theme-reveal-dark')")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'agent-approval-mobile.png'))
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.screenshot(path=str(tmp_path / 'agent-approval-alternate-theme.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.get_by_role('button', name='同意执行' if approve else '拒绝', exact=True).click()
    playwright.expect(page.locator('#aiFeedback')).to_have_text('可继续追问', timeout=10000)
    assert len(peer['exec']) == (2 if approve else 1)
    playwright.expect(page.locator('.tai-steps')).to_contain_text('已完成' if approve else '未执行')
    assert len(model_server.calls) == 3
    assert not peer['input']
    page.locator('#aiPanelClose').click()
    page.locator('#sshDisconnect').click()


def test_real_insecure_http_requires_confirmation_then_supports_claude_and_ssh(page, http_server, manager, peer, ai_manager, model_server, monkeypatch, tmp_path):
    import ssh_terminal
    stream = ssh_terminal.Session.stream
    monkeypatch.setattr(ssh_terminal.Session, 'stream', lambda self, cursor: stream(self, cursor, duration=.2))
    origin = 'http://mini-deploy.test:' + http_server.origin.rsplit(':', 1)[1]
    # Forward the reserved test origin to the real server, retaining Host,
    # cookies and Origin. The browser still treats the page as insecure HTTP.
    def forward(route):
        headers = {**route.request.all_headers(), 'host': origin.removeprefix('http://')}
        response = route.fetch(url=http_server.origin + route.request.url.removeprefix(origin),
                               headers=headers, max_redirects=0)
        route.fulfill(response=response)
    page.route(origin + '/**', forward)
    page.context.add_cookies([{'name': agent.COOKIE_NAME, 'value': http_server.cookie, 'url': origin}])
    response = page.goto(origin + '/ui?view=terminal')
    assert page.locator('#terminalView').count(), (response.status, page.content()[:1500])
    assert page.evaluate('window.isSecureContext') is False
    playwright.expect(page.locator('#sshHost')).to_be_disabled()
    playwright.expect(page.locator('#sshAllowHttp')).to_be_disabled()
    page.screenshot(path=str(tmp_path / 'http-confirmation.png'), full_page=True)
    page.get_by_role('button', name='模型设置', exact=True).click()
    playwright.expect(page.locator('#aiSave')).to_be_disabled()
    page.get_by_role('button', name='关闭模型设置', exact=True).click()
    page.locator('#sshHttpCheck').check()
    page.locator('#sshAllowHttp').click()
    playwright.expect(page.locator('#sshHost')).to_be_enabled()
    playwright.expect(page.locator('#sshHttpTitle')).to_contain_text('本次登录')
    playwright.expect(page.locator('#sshHttpConsent')).not_to_be_visible()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'http-mobile-enabled.png'), full_page=True)
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.get_by_role('button', name='模型设置', exact=True).click()
    page.get_by_role('combobox', name='接口类型', exact=True).click()
    page.get_by_role('option', name='Anthropic / Claude 原生', exact=True).click()
    page.locator('#aiBaseUrl').fill(model_server.origin + '/v1')
    page.locator('#aiModel').fill('test-model')
    page.locator('#aiKey').fill('test-key-only')
    page.locator('#aiTest').click()
    playwright.expect(page.locator('#aiSettingsFeedback')).to_contain_text('连接成功', timeout=10000)
    assert model_server.calls[0][0] == '/v1/messages'
    page.locator('#aiSave').click()
    playwright.expect(page.locator('#aiSettingsDialog')).not_to_be_visible()
    page.reload()
    playwright.expect(page.locator('#sshHost')).to_be_enabled()
    fill(page, peer)
    page.locator('#sshConnect').click()
    page.locator('#sshTrustCheck').check()
    page.locator('#sshTrustConfirm').click()
    playwright.expect(page.locator('#sshState')).to_have_text('已连接')
    playwright.expect(page.locator('.xterm-rows')).to_contain_text('本地测试终端')
    page.locator('.xterm-helper-textarea').focus()
    page.keyboard.type('http-input')
    playwright.expect(page.locator('.xterm-rows')).to_contain_text('http-input')
    page.locator('#aiChatOpen').click()
    page.locator('#aiQuestion').fill('帮我看看终端')
    page.locator('#aiAskSend').click()
    playwright.expect(page.locator('#aiFeedback')).to_have_text('可继续追问', timeout=10000)
    assert model_server.calls[-1][0] == '/v1/messages'
    assert 'http-input' in json.dumps(model_server.calls[-1][2])
    page.locator('#aiPanelClose').click()
    page.locator('#sshDisconnect').click()
    page.goto(origin + '/logout')
    page.context.add_cookies([{'name': agent.COOKIE_NAME, 'value': agent._make_session_cookie(), 'url': origin}])
    page.goto(origin + '/ui?view=terminal')
    playwright.expect(page.locator('#sshHost')).to_be_disabled()
    playwright.expect(page.locator('#sshHttpConsent')).to_be_visible()
