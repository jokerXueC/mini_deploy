"""Real model protocol loops, scoped grants, single-use approval and SSH channels."""
import base64
import json
import threading
import time

import pytest

import terminal_ai as ai
import terminal_tools as tools
import test_terminal_ai as ai_tests
import test_ssh_terminal as ssh_tests
from test_ssh_terminal import request

ai_manager = ai_tests.manager
model_server = ai_tests.model_server
manager = ssh_tests.manager
peer = ssh_tests.peer
http_server = ssh_tests.http_server
runtime = ssh_tests.runtime


def tool_response(protocol, name, arguments, call_id='call_one'):
    if protocol == 'anthropic':
        return {'content': [{'type': 'tool_use', 'id': call_id, 'name': name, 'input': arguments}],
                'usage': {'input_tokens': 10, 'output_tokens': 5}}
    return {'choices': [{'message': {'content': None, 'tool_calls': [{
        'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}}],
        'usage': {'total_tokens': 15}}


def answer(protocol):
    text = '已检查容器状态，建议先核对近期日志。'
    return {'content': [{'type': 'text', 'text': text}]} if protocol == 'anthropic' else {
        'choices': [{'message': {'content': text}}]}


def wait_status(manager, owner, key, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = manager.job(owner, key)
        if result['status'] in expected:
            return result
        time.sleep(.01)
    raise AssertionError(f'Task did not reach {expected}: {result}')


def submit(manager, *, mode='readonly', execute=None, validate=None, **data):
    if mode != 'readonly':
        manager.set_access('owner', 'ssh', {'mode': mode, 'consent': True})
    return manager.submit('owner', action='chat', session_id='ssh', target='demo@test:22',
                          executor=execute or (lambda plan, cancel: {'exit_code': 0, 'output': 'healthy'}),
                          validate=validate, data=ai_tests.chat_data(manager, permission=mode, **data))


@pytest.mark.parametrize('name,args', [
    ('execute_shell', {'command': 'rm -rf /'}), ('get_container_logs', {'container': '-f'}),
    ('get_container_logs', {'container': 'web; rm -rf /'}), ('get_service_logs', {'service': 'nginx\nreboot'}),
    ('restart_service', {'service': '/tmp/evil.service'}), ('restart_container', {'container': 'web'}),
    ('get_container_logs', {'container': 'web', 'lines': True}),
    ('get_container_logs', {'container': 'web', 'lines': 201}),
    ('get_service_status', {'service': 'nginx', 'command': 'reboot'}),
])
def test_tools_reject_arbitrary_commands_options_paths_and_unbounded_logs(name, args):
    with pytest.raises(tools.ToolError):
        tools.plan(name, args)


def test_tool_schemas_and_plans_keep_modifications_explicit():
    assert all(not tools.TOOLS[t['function']['name']][-1] for t in tools.schemas())
    assert len(tools.schemas(True)) == 11
    assert tools.plan('restart_service', {'service': 'nginx'})['arguments'] == {'service': 'nginx.service'}
    assert tools.plan('get_container_logs', {'container': 'web'})['arguments']['lines'] == 100
    assert 'Environment' not in tools.plan('get_container_status', {'container': 'web'})['command']


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_agent_real_protocol_tool_results_followup_and_redaction(ai_manager, model_server, protocol):
    ai_manager.save(ai_tests.config(model_server, protocol))
    model_server.content = lambda body, n: tool_response(protocol, 'list_containers', {}) if n == 1 else answer(protocol)
    executed = []
    def execute(plan, cancel):
        executed.append(plan)
        return {'exit_code': 0, 'output': 'container healthy\npassword=private-value\ntest-key-only'}
    job = submit(ai_manager, execute=execute)
    result = wait_status(ai_manager, 'owner', job['id'], {'done', 'failed'})
    assert result['status'] == 'done' and len(executed) == 1
    assert result['events'][0]['status'] == 'done' and result['target'] == 'demo@test:22'
    assert 'private-value' not in json.dumps(result) and 'test-key-only' not in json.dumps(result)
    first, second = model_server.calls[0][2], model_server.calls[1][2]
    assert first['tools']
    if protocol == 'anthropic':
        assert first['tools'][0]['input_schema']
        assert second['messages'][-2]['content'][0]['type'] == 'tool_use'
        assert second['messages'][-1]['content'][0]['type'] == 'tool_result'
        assert second['messages'][-1]['content'][0]['tool_use_id'] == 'call_one'
    else:
        assert first['tools'][0]['function']['parameters']
        assert second['messages'][-2]['tool_calls'][0]['id'] == 'call_one'
        assert second['messages'][-1]['role'] == 'tool' and second['messages'][-1]['tool_call_id'] == 'call_one'
    assert 'container healthy' in json.dumps(second) and 'private-value' not in json.dumps(second)
    followup = submit(ai_manager, conversation_id=job['conversation_id'])
    assert wait_status(ai_manager, 'owner', followup['id'], {'done', 'failed'})['status'] == 'done'
    assert 'container healthy' in json.dumps(model_server.calls[-1][2])


@pytest.mark.parametrize('decision', [True, False])
def test_approval_is_bound_to_owner_target_operation_and_consumed_once(ai_manager, model_server, decision):
    ai_manager.save(ai_tests.config(model_server))
    model_server.content = lambda body, n: tool_response('openai', 'restart_service', {'service': 'nginx'}) if n == 1 else answer('openai')
    executed = []
    job = submit(ai_manager, mode='approve', execute=lambda plan, cancel: executed.append(plan) or {'exit_code': 0})
    pending = wait_status(ai_manager, 'owner', job['id'], {'awaiting_approval'})
    assert not executed and pending['approval']['target'] == 'demo@test:22'
    assert 'nginx.service' in pending['approval']['command']
    with pytest.raises(ai.AIError):
        ai_manager.approve('other', job['id'], pending['approval']['id'], True)
    with pytest.raises(ai.AIError):
        ai_manager.approve('owner', job['id'], 'forged', True)
    with pytest.raises(ai.AIError):
        ai_manager.approve('owner', job['id'], pending['approval']['id'], 'true')
    with pytest.raises(ai.AIError) as error:
        submit(ai_manager)
    assert error.value.status == 409
    ai_manager.approve('owner', job['id'], pending['approval']['id'], decision)
    with pytest.raises(ai.AIError):
        ai_manager.approve('owner', job['id'], pending['approval']['id'], True)
    result = wait_status(ai_manager, 'owner', job['id'], {'done', 'failed'})
    assert result['status'] == 'done' and len(executed) == int(decision)
    assert result['events'][0]['status'] == ('done' if decision else 'blocked')


@pytest.mark.parametrize('mode,automatic,expected', [('readonly', False, 0), ('auto', False, 1), ('auto', True, 0)])
def test_auto_requires_grant_and_background_followup_cannot_modify(ai_manager, model_server, mode, automatic, expected):
    ai_manager.save(ai_tests.config(model_server))
    model_server.content = lambda body, n: tool_response('openai', 'restart_container', {'container': 'a' * 64}) if n == 1 else answer('openai')
    executed = []
    job = submit(ai_manager, mode=mode, automatic=automatic,
                 execute=lambda plan, cancel: executed.append(plan) or {'exit_code': 0})
    assert wait_status(ai_manager, 'owner', job['id'], {'done', 'failed'})['status'] == 'done'
    assert len(executed) == expected
    for kwargs in [{'mode': 'auto'}, {'mode': 'approve', 'consent': 'true'}]:
        with pytest.raises(ai.AIError):
            ai_manager.set_access('owner', 'different-ssh', kwargs)
    assert ai_manager.access('other', 'ssh')['mode'] == 'readonly'
    assert ai_manager.access('owner', 'different-ssh')['mode'] == 'readonly'


@pytest.mark.parametrize('operation', ['cancel', 'revoke', 'disconnect', 'model', 'expire'])
def test_pending_approval_stops_on_cancel_revocation_disconnect_model_or_expiry(ai_manager, model_server, operation):
    ai_manager.save(ai_tests.config(model_server))
    model_server.content = lambda body, n: tool_response('openai', 'restart_service', {'service': 'nginx'})
    executed = []
    job = submit(ai_manager, mode='approve', execute=lambda plan, cancel: executed.append(plan) or {'exit_code': 0})
    pending = wait_status(ai_manager, 'owner', job['id'], {'awaiting_approval'})
    if operation == 'cancel':
        ai_manager.cancel('owner', job['id'])
    elif operation == 'revoke':
        ai_manager.set_access('owner', 'ssh', {'mode': 'readonly'})
    elif operation == 'disconnect':
        ai_manager.cancel_session('owner', 'ssh')
    elif operation == 'model':
        ai_manager.save({**ai_tests.config(model_server), 'model': 'changed-model'})
    else:
        ai_manager.permissions[('owner', 'ssh')]['expires'] -= 1801
    result = wait_status(ai_manager, 'owner', job['id'], {'cancelled', 'failed'})
    assert not executed and not result.get('approval')
    with pytest.raises(ai.AIError):
        ai_manager.approve('owner', job['id'], pending['approval']['id'], True)


def test_tool_loop_bounded_and_repeat_modifications_not_retried(ai_manager, model_server):
    ai_manager.save(ai_tests.config(model_server))
    model_server.content = lambda body, n: tool_response('openai', 'restart_service', {'service': 'nginx'}, f'call_{n}')
    executed = []
    job = submit(ai_manager, mode='auto', execute=lambda plan, cancel: executed.append(plan) or {'exit_code': 1, 'output': 'failed'})
    result = wait_status(ai_manager, 'owner', job['id'], {'done', 'failed'})
    assert result['status'] == 'failed' and len(model_server.calls) == 6 and len(executed) == 1
    assert len(result['events']) == 6 and all(e['status'] == 'blocked' for e in result['events'][1:])
    model_server.content = answer('openai')
    followup = submit(ai_manager, mode='auto', conversation_id=job['conversation_id'])
    assert wait_status(ai_manager, 'owner', followup['id'], {'done'})['status'] == 'done'
    previous = model_server.calls[-1][2]['messages']
    assert 'failed' in json.dumps(previous) and '未完成' in json.dumps(previous, ensure_ascii=False)


def test_cancel_during_model_call_prevents_later_tools(ai_manager, model_server):
    ai_manager.save(ai_tests.config(model_server))
    model_server.gate = threading.Event()
    model_server.content = tool_response('openai', 'list_containers', {})
    executed = []
    job = submit(ai_manager, execute=lambda plan, cancel: executed.append(plan) or {'exit_code': 0})
    ai_manager.cancel('owner', job['id'])
    model_server.gate.set()
    assert wait_status(ai_manager, 'owner', job['id'], {'cancelled'})['status'] == 'cancelled'
    assert not executed


def test_permission_mismatch_and_expired_auto_do_not_silently_execute(ai_manager, model_server):
    ai_manager.save(ai_tests.config(model_server))
    with pytest.raises(ai.AIError) as error:
        ai_manager.submit('owner', action='chat', session_id='ssh', executor=lambda *args: None,
                          data=ai_tests.chat_data(ai_manager, permission='auto'))
    assert error.value.status == 409 and not model_server.calls
    ai_manager.set_access('owner', 'ssh', {'mode': 'auto', 'consent': True})
    ai_manager.permissions[('owner', 'ssh')]['expires'] -= 1801
    with pytest.raises(ai.AIError):
        ai_manager.submit('owner', action='chat', session_id='ssh', executor=lambda *args: None,
                          data=ai_tests.chat_data(ai_manager, permission='auto'))
    assert not model_server.calls


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_multiple_tool_results_match_ids_and_stop_at_eight_calls(ai_manager, model_server, protocol):
    ai_manager.save(ai_tests.config(model_server, protocol))
    def respond(body, n):
        result = tool_response(protocol, 'list_containers', {}, f'call_{n}_0')
        for i in range(1, 4):
            extra = tool_response(protocol, 'get_listening_ports', {}, f'call_{n}_{i}')
            if protocol == 'anthropic':
                result['content'].extend(extra['content'])
            else:
                result['choices'][0]['message']['tool_calls'].extend(extra['choices'][0]['message']['tool_calls'])
        return result
    model_server.content = respond
    executed = []
    job = submit(ai_manager, execute=lambda plan, cancel: executed.append(plan) or {'exit_code': 0, 'output': 'ok'})
    result = wait_status(ai_manager, 'owner', job['id'], {'done', 'failed'})
    assert result['status'] == 'failed' and len(executed) == 8 and len(result['events']) == 8
    messages = model_server.calls[1][2]['messages']
    if protocol == 'anthropic':
        assert len(messages[-1]['content']) == 4
        assert [m['tool_use_id'] for m in messages[-1]['content']] == [f'call_1_{i}' for i in range(4)]
    else:
        assert [m['tool_call_id'] for m in messages[-4:]] == [f'call_1_{i}' for i in range(4)]


def test_duplicate_call_ids_and_malformed_calls_never_repeat_operations(ai_manager, model_server):
    ai_manager.save(ai_tests.config(model_server))
    model_server.content = tool_response('openai', 'list_containers', {})
    executed = []
    job = submit(ai_manager, execute=lambda plan, cancel: executed.append(plan) or {'exit_code': 0})
    assert wait_status(ai_manager, 'owner', job['id'], {'failed'})['status'] == 'failed'
    assert len(executed) == 1
    malformed = tool_response('openai', 'list_containers', {})
    malformed['choices'][0]['message']['tool_calls'][0]['id'] = 'id\nunsafe'
    model_server.content = malformed
    job = submit(ai_manager, execute=lambda plan, cancel: executed.append(plan) or {'exit_code': 0})
    assert wait_status(ai_manager, 'owner', job['id'], {'failed'})['status'] == 'failed'
    assert len(executed) == 1


def test_uncertain_write_failure_prevents_other_modifications(ai_manager, model_server):
    ai_manager.save(ai_tests.config(model_server))
    model_server.content = lambda body, n: (
        tool_response('openai', 'restart_service', {'service': 'nginx'}, 'first') if n == 1 else
        tool_response('openai', 'reload_service', {'service': 'nginx'}, 'second') if n == 2 else answer('openai'))
    executed = []
    def execute(plan, cancel):
        executed.append(plan)
        raise ssh_tests.ssh.SSHError('超时，已提交的操作可能仍在执行。', 504)
    job = submit(ai_manager, mode='auto', execute=execute)
    result = wait_status(ai_manager, 'owner', job['id'], {'done', 'failed'})
    assert result['status'] == 'done' and len(executed) == 1
    assert [e['status'] for e in result['events']] == ['failed', 'blocked']


def test_logout_clears_grants_and_finished_task_outputs(ai_manager, model_server):
    ai_manager.save(ai_tests.config(model_server))
    job = submit(ai_manager, mode='auto')
    wait_status(ai_manager, 'owner', job['id'], {'done'})
    ai_manager.revoke('owner')
    assert not ai_manager.permissions and not ai_manager.conversations
    with pytest.raises(ai.AIError):
        ai_manager.job('owner', job['id'])


def test_chat_only_omits_tools_and_incompatible_model_has_actionable_error(ai_manager, model_server):
    ai_manager.save(ai_tests.config(model_server))
    job = submit(ai_manager, mode='chat')
    assert wait_status(ai_manager, 'owner', job['id'], {'done', 'failed'})['status'] == 'done'
    assert 'tools' not in model_server.calls[0][2]
    model_server.status = 400
    ai_manager.set_access('owner', 'ssh', {'mode': 'readonly'})
    job = submit(ai_manager)
    result = wait_status(ai_manager, 'owner', job['id'], {'done', 'failed'})
    assert result['status'] == 'failed' and '仅对话' in result['detail']


def test_real_ssh_tool_channel_is_separate_cancelable_and_bounded(manager, peer):
    session = ssh_tests.connect(manager, peer)
    result = session.execute_tool(tools.plan('list_containers', {})['command'], threading.Event())
    assert result['exit_code'] == 0 and 'mock inspection' in result['output']
    assert b'list-units' not in b''.join(peer['input']) and not session.closed
    assert '本地测试终端' in base64.b64decode(session.poll(0, wait=0)['data']).decode()
    peer['exec_output'] = b'x' * 50000
    result = session.execute_tool('docker ps', threading.Event(), max_bytes=100)
    assert result['truncated'] and len(result['output']) == 100 and not session.closed
    peer['exec_hang'] = True
    started = time.monotonic()
    with pytest.raises(ssh_tests.ssh.SSHError):
        session.execute_tool('docker ps', threading.Event(), timeout=.25)
    assert time.monotonic() - started < 1.5 and not session.closed
    cancel = threading.Event()
    threading.Timer(.1, cancel.set).start()
    with pytest.raises(ssh_tests.ssh.SSHError):
        session.execute_tool('docker ps', cancel, timeout=2)
    assert not session.closed and session.transport.is_active()


def test_agent_routes_use_owned_session_csrf_transport_and_single_use_approval(http_server, manager, peer, ai_manager, model_server):
    ai_manager.save(ai_tests.config(model_server))
    owner = ai.owner(http_server.cookie)
    session = ssh_tests.connect(manager, peer, owner)
    data = {'session_id': session.id, 'mode': 'approve', 'consent': True}
    for kwargs in [{'authenticated': False}, {'csrf': False}, {'origin': False}]:
        assert request(http_server, 'POST', '/terminal-ai/access', data, **kwargs)[0] in {401, 403}
    assert request(http_server, 'POST', '/terminal-ai/access', {**data, 'session_id': 'foreign'})[0] == 404
    assert request(http_server, 'POST', '/terminal-ai/access', {**data, 'consent': False})[0] == 400
    assert request(http_server, 'POST', '/terminal-ai/access', data)[0] == 200
    assert request(http_server, 'GET', '/terminal-ai/access?session_id=' + session.id)[1]['mode'] == 'approve'
    model_server.content = lambda body, n: tool_response('openai', 'restart_service', {'service': 'nginx'}) if n == 1 else answer('openai')
    status, job = request(http_server, 'POST', '/terminal-ai/chat', ai_tests.chat_data(
        ai_manager, session_id=session.id, permission='approve'))
    assert status == 200
    pending = wait_status(ai_manager, owner, job['id'], {'awaiting_approval'})
    assert not peer['exec']
    approval = {'id': job['id'], 'approval_id': pending['approval']['id'], 'approve': True}
    assert request(http_server, 'POST', '/terminal-ai/approve', approval, csrf=False)[0] == 403
    assert request(http_server, 'POST', '/terminal-ai/approve', approval)[0] == 200
    assert wait_status(ai_manager, owner, job['id'], {'done', 'failed'})['status'] == 'done'
    assert len(peer['exec']) == 1 and b'PT' not in b''.join(peer['input'])
    assert request(http_server, 'POST', '/terminal-ai/approve', approval)[0] == 409
    assert request(http_server, 'POST', '/terminal-ai/cancel', {'id': 'foreign'})[0] == 404
    assert request(http_server, 'POST', '/ssh/disconnect', {'id': session.id})[0] == 200
    assert ai_manager.access(owner, session.id)['mode'] == 'readonly'
