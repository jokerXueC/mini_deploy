"""Global conversations discover targets and preserve SSH ownership and approvals."""
import json
import hashlib
import threading
import time
from types import SimpleNamespace

import pytest

import agent
import panel_assistant
import terminal_ai
import terminal_tools
import test_terminal_ai as ai_tests
import test_ssh_terminal as ssh_tests
from test_terminal_agent import tool_response, answer, wait_status
from test_panel_diagnostics import evidence as evidence, manager as manager, model_server as model_server
from test_deployment_removal import http_server as http_server, runtime as runtime
from test_ssh_terminal import request, peer as peer
from ssh_sftp_peer import enable_sftp

ssh_manager = ssh_tests.manager


def backend(evidence, **options):
    sessions = {'owned': SimpleNamespace(spec={'username': 'ops', 'host': 'target.test', 'port': 22},
                closed=False, expires=time.time() + 60)}
    def resolve(key):
        if key not in sessions:
            raise terminal_tools.ToolError('连接不属于当前登录')
        return sessions[key]
    return panel_assistant.Backend(evidence.backend({'kind': 'overview'}), mode=options.pop('mode', 'approve'),
        commands=True, sessions=lambda: [{'id': key} for key in sessions], resolve=resolve, projects=lambda: [], **options), sessions


def test_targets_are_discovered_and_foreign_expired_sessions_never_execute(evidence):
    assistant, sessions = backend(evidence)
    data = json.loads(assistant.execute(assistant.plan('list_connected_servers', {}), threading.Event())['output'])
    assert data['servers'] == [{'session_id': 'owned', 'username': 'ops', 'host': 'target.test', 'port': 22, 'name': None}]
    with pytest.raises(terminal_tools.ToolError):
        assistant.plan('ssh_get_service_status', {'session_id': 'foreign', 'service': 'nginx'})
    sessions['owned'].expires = time.time() - 1
    with pytest.raises(terminal_tools.ToolError, match='过期'):
        assistant.plan('ssh_get_service_status', {'session_id': 'owned', 'service': 'nginx'})


def test_auto_grant_does_not_automatically_cover_new_connections_or_deletion(evidence):
    assistant, sessions = backend(evidence, mode='auto', auto_sessions=['owned'])
    args = {'session_id': 'owned', 'service': 'nginx'}
    assert not assistant.plan('ssh_restart_service', args).get('force_approval')
    sessions['new'] = sessions['owned']
    assert assistant.plan('ssh_restart_service', {**args, 'session_id': 'new'})['approval_required']
    with pytest.raises(terminal_tools.ToolError):
        assistant.plan('ssh_execute_command', {'session_id': 'owned', 'command': 'rm -rf /opt/app'})
    readonly, _ = backend(evidence, mode='readonly')
    with pytest.raises(terminal_tools.ToolError):
        readonly.plan('ssh_restart_service', args)


def test_global_evidence_advertises_current_tools_and_terminal_output_is_redacted(evidence):
    assistant, sessions = backend(evidence)
    plan = assistant.initial_call()['function']
    initial = json.loads(assistant.execute(assistant.plan(plan['name'], json.loads(plan['arguments'])), threading.Event())['output'])
    assert {'ssh_read_text_file', 'ssh_execute_command', 'list_connected_servers'} <= set(initial['available_tools'])
    assert set(initial['available_tools']) == {tool['function']['name'] for tool in assistant.schemas()}
    assert '下拉' in initial['code_scope']['notice']
    sessions['owned'].id = 'owned'
    sessions['owned'].condition = threading.Condition()
    sessions['owned'].chunks = [(1, b'port=8080\npassword=private-terminal\n')]
    output = assistant.execute(assistant.plan('ssh_read_terminal_output', {'session_id': 'owned'}), threading.Event())
    assert 'port=8080' in output['output'] and 'private-terminal' not in output['output']
    capabilities = json.loads(assistant.execute(assistant.plan('get_read_capabilities', {}), threading.Event())['output'])
    assert capabilities['host_read_only'] and capabilities['ssh_permission'] == 'approve'


@pytest.mark.parametrize('revoke', [False, True])
def test_global_pending_approval_cannot_execute_after_disconnect_or_revocation(
        http_server, manager, model_server, evidence, ssh_manager, peer, monkeypatch, revoke):
    manager.save(ai_tests.config(model_server))
    owner = terminal_ai.owner(http_server.cookie)
    session = ssh_tests.connect(ssh_manager, peer, owner)
    monkeypatch.setattr(agent, '_ssh_manager', lambda: ssh_manager)
    model_server.content = lambda body, count: tool_response('openai', 'ssh_restart_service',
        {'session_id': session.id, 'service': 'nginx'}) if count == 1 else answer('openai')
    status, job = request(http_server, 'POST', '/terminal-ai/global', ai_tests.chat_data(manager, permission='approve'))
    assert status == 200
    pending = wait_status(manager, owner, job['id'], {'awaiting_approval'})
    if revoke:
        manager.cancel_session(owner, panel_assistant.SESSION)
    else:
        session.close('test disconnect')
    request(http_server, 'POST', '/terminal-ai/approve',
        {'id': job['id'], 'approval_id': pending['approval']['id'], 'approve': True})
    ai_tests.finished(manager, owner, job['id'])
    assert not peer['exec']


@pytest.mark.parametrize('recover', [False, True])
def test_global_file_edit_previews_backs_up_and_verifies_the_same_remote(
        http_server, manager, model_server, evidence, ssh_manager, peer, monkeypatch, tmp_path, recover):
    manager.save(ai_tests.config(model_server))
    owner = terminal_ai.owner(http_server.cookie)
    session = ssh_tests.connect(ssh_manager, peer, owner)
    monkeypatch.setattr(agent, '_ssh_manager', lambda: ssh_manager)
    root = tmp_path / 'remote'
    root.mkdir()
    path = root / 'app.conf'
    path.write_text('port=8080\n', encoding='utf-8')
    enable_sftp(peer, root)
    def response(body, count):
        if count == 1:
            return tool_response('openai', 'ssh_edit_file', {
                'session_id': session.id, 'path': '/app.conf', 'old_text': '8080', 'new_text': '8081',
                'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'reason': '调整端口'})
        if recover and count == 2:
            return tool_response('openai', 'ssh_restart_service', {'session_id': session.id, 'service': 'nginx'}, 'restart')
        if recover and count == 3:
            results = [json.loads(m['content']) for m in body['messages'] if m['role'] == 'tool']
            recovery = next(result['recovery'] for result in results if 'recovery' in result)
            return tool_response('openai', 'ssh_restore_file', {'session_id': session.id, **recovery}, 'restore')
        return answer('openai')
    model_server.content = response
    status, job = request(http_server, 'POST', '/terminal-ai/global', ai_tests.chat_data(manager, permission='approve'))
    assert status == 200
    pending = wait_status(manager, owner, job['id'], {'awaiting_approval'})
    assert pending['approval']['target'] == f"{session.spec['username']}@{session.spec['host']}:{session.spec['port']}"
    assert '+port=8081' in pending['approval']['preview']['diff']
    assert path.read_text() == 'port=8080\n'
    assert request(http_server, 'POST', '/terminal-ai/approve',
        {'id': job['id'], 'approval_id': pending['approval']['id'], 'approve': True})[0] == 200
    if recover:
        for operation in ('restart_service', 'restore_file'):
            pending = wait_status(manager, owner, job['id'], {'awaiting_approval'})
            assert pending['events'][-1]['write']
            if operation == 'restart_service':
                assert ' restart ' in pending['approval']['command']
            if operation == 'restore_file':
                assert '+port=8080' in pending['approval']['preview']['diff']
            assert request(http_server, 'POST', '/terminal-ai/approve',
                {'id': job['id'], 'approval_id': pending['approval']['id'], 'approve': True})[0] == 200
    done = ai_tests.finished(manager, owner, job['id'])
    assert done['status'] == 'done' and path.read_text() == ('port=8080\n' if recover else 'port=8081\n')
    assert len(list(root.glob('*.mini-deploy-backup.*'))) == (2 if recover else 1)
    edited = next(item for item in done['events'] if item.get('write'))
    assert edited['verification']['status'] == 'passed' and edited['recovery']


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_one_conversation_reads_panel_then_real_ssh_and_requires_targeted_approval(
        http_server, manager, model_server, evidence, ssh_manager, peer, monkeypatch, protocol):
    manager.save(ai_tests.config(model_server, protocol))
    owner = terminal_ai.owner(http_server.cookie)
    session = ssh_tests.connect(ssh_manager, peer, owner)
    monkeypatch.setattr(agent, '_ssh_manager', lambda: ssh_manager)
    sequence = [('list_connected_servers', {}),
                ('ssh_get_service_status', {'session_id': session.id, 'service': 'nginx'}),
                ('ssh_restart_service', {'session_id': session.id, 'service': 'nginx'})]
    def responses(body, count):
        names = {tool.get('name') or tool.get('function', {}).get('name') for tool in body['tools']}
        assert {'list_connected_servers', 'read_host_file', 'query_database', 'ssh_restart_service'} <= names
        assert '当前工具目标' in json.dumps(body, ensure_ascii=False)
        if count <= len(sequence):
            name, args = sequence[count - 1]
            return tool_response(protocol, name, args, call_id=f'global-{count}')
        return answer(protocol)
    model_server.content = responses
    data = ai_tests.chat_data(manager, permission='approve', question='查看本机资源，再检查并重启已连接服务器的 nginx')
    status, job = request(http_server, 'POST', '/terminal-ai/global', data)
    assert status == 200, job
    pending = wait_status(manager, owner, job['id'], {'awaiting_approval'})
    assert pending['approval']['target'] == f"{session.spec['username']}@{session.spec['host']}:{session.spec['port']}"
    assert len(peer['exec']) == 1 and b'restart' not in b''.join(peer['exec'])
    approval = {'id': job['id'], 'approval_id': pending['approval']['id'], 'approve': True}
    assert request(http_server, 'POST', '/terminal-ai/approve', approval)[0] == 200
    done = ai_tests.finished(manager, owner, job['id'])
    assert done['status'] == 'done', done
    assert len(peer['exec']) == 3 and b'restart' in peer['exec'][1] and b'systemctl show' in peer['exec'][2]
    assert request(http_server, 'POST', '/terminal-ai/approve', approval)[0] == 409
    model_server.content = answer(protocol)
    status, followup = request(http_server, 'POST', '/terminal-ai/global', {
        **data, 'question': '再检查业务数据库', 'conversation_id': job['conversation_id'], 'history_id': job['history_id']})
    assert status == 200
    assert ai_tests.finished(manager, owner, followup['id'])['status'] == 'done'
    assert followup['history_id'] == job['history_id']
    assert len(manager.history.detail(job['history_id'])['turns']) == 2


def test_global_routes_check_auth_csrf_scope_and_explicit_auto_grant(
        http_server, manager, model_server, evidence, ssh_manager, peer, monkeypatch):
    manager.save(ai_tests.config(model_server))
    monkeypatch.setattr(agent, '_ssh_manager', lambda: ssh_manager)
    data = ai_tests.chat_data(manager, permission='approve')
    for kwargs in [{'authenticated': False}, {'origin': False}, {'csrf': False}]:
        assert request(http_server, 'POST', '/terminal-ai/global', data, **kwargs)[0] in {401, 403}
    assert request(http_server, 'POST', '/terminal-ai/global', {**data, 'session_id': 'foreign'})[0] == 400
    grant = {'session_id': panel_assistant.SESSION, 'mode': 'auto', 'policy_version': 2, 'consent': True}
    assert request(http_server, 'POST', '/terminal-ai/access', grant)[0] == 400
    owner = terminal_ai.owner(http_server.cookie)
    session = ssh_tests.connect(ssh_manager, peer, owner)
    assert request(http_server, 'POST', '/terminal-ai/access', {**grant, 'consent': False})[0] == 400
    assert request(http_server, 'POST', '/terminal-ai/access', grant)[0] == 200
    assert manager.permissions[(owner, panel_assistant.SESSION)]['global_sessions'] == [session.id]
    assert not model_server.calls
