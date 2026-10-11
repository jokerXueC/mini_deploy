"""Actual local model HTTP contracts, credential boundaries and SSH ownership."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import agent
import terminal_ai as ai
from test_deployment_removal import http_server as http_server, runtime as runtime
from test_ssh_terminal import request


@pytest.fixture
def model_server():
    state = SimpleNamespace(calls=[], status=200, content=None, gate=None)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state.calls.append((self.path, dict(self.headers), body))
            if state.gate:
                state.gate.wait(3)
            result = state.content
            if callable(result):
                result = result(body, len(state.calls))
            if result is None:
                result = ({'content': [{'type': 'text', 'text': '检查容器网络\n```sh\ndocker ps\n```'}],
                           'usage': {'input_tokens': 10, 'output_tokens': 5}} if self.path.endswith('/messages')
                          else {'choices': [{'message': {'content': '检查容器网络\n```sh\ndocker ps\n```'}}],
                                'usage': {'total_tokens': 15}})
            encoded = json.dumps(result).encode()
            self.send_response(state.status)
            if 300 <= state.status < 400:
                self.send_header('Location', state.origin + '/stolen')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    state.origin = f'http://127.0.0.1:{server.server_port}'
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield state
    server.shutdown()
    server.server_close()
    worker.join(3)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    instance = ai.Manager(tmp_path / 'ai')
    monkeypatch.setattr(agent, '_ai_manager_instance', instance)
    monkeypatch.setattr(agent, '_ai_manager', lambda: instance)
    yield instance
    instance.shutdown()


def config(server, protocol='openai'):
    return {'protocol': protocol, 'base_url': server.origin + '/v1', 'model': 'test-model', 'api_key': 'test-key-only'}


def finished(manager, owner, key):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = manager.job(owner, key)
        if result['status'] != 'running':
            return result
        time.sleep(.01)
    raise AssertionError('Model task did not finish')


@pytest.mark.parametrize('value', ['http://example.com/v1', 'ftp://localhost', 'https://user:secret@example.com',
                                  'https://example.com?key=secret', 'https://example.com#secret', '',
                                  'https://example.com:0', 'https://example.com\nsecret'])
def test_unsafe_endpoint_rejected(value):
    with pytest.raises(ai.AIError):
        ai.endpoint(value)


def test_endpoint_normalizes_without_duplicate_paths():
    assert ai.endpoint('https://api.example.com/v1/') == 'https://api.example.com/v1/chat/completions'
    assert ai.endpoint('https://api.example.com/v1/messages', 'anthropic').endswith('/v1/messages')
    assert ai.endpoint('http://127.0.0.1:11434/v1').endswith('/v1/chat/completions')
    assert ai.endpoint('https://api.example.com/v1/chat/completions').endswith('/v1/chat/completions')


def test_config_survives_restart_and_never_returns_key(manager, model_server):
    result = manager.save(config(model_server))
    assert result['has_key'] and 'test-key-only' not in json.dumps(result)
    manager.save({**config(model_server), 'api_key': ''})
    restarted = ai.Manager(manager.directory)
    try:
        assert restarted.settings() == result
        assert restarted._config()['api_key'] == 'test-key-only'
    finally:
        restarted.shutdown()
    manager.save({**config(model_server), 'clear_key': True})
    assert not manager.settings()['has_key']


def test_key_not_reused_for_different_provider(manager, model_server):
    manager.save(config(model_server))
    with pytest.raises(ai.AIError, match='重新填写'):
        manager.save({**config(model_server), 'protocol': 'anthropic', 'api_key': ''})
    assert manager.settings()['protocol'] == 'openai'


def test_corrupt_config_is_not_overwritten(manager):
    manager.directory.mkdir()
    manager.path.write_text('{broken')
    with pytest.raises(ai.AIError, match='原文件已保留'):
        manager.save({'base_url': 'https://example.com/v1', 'model': 'a'})
    assert manager.path.read_text() == '{broken'


def test_redaction_preserves_errors_and_removes_common_credentials():
    text = '\x1b[31mERROR DNS failed\x1b[0m\nPASSWORD="two secret words"\nAuthorization: Bearer hidden-token\n'
    text += 'postgres://root:database-password@postgres/app\nCookie: sid=first; token=second\n'
    text += '-----BEGIN OPENSSH PRIVATE KEY-----\nprivate bytes\n-----END OPENSSH PRIVATE KEY-----'
    result = ai.redact(text)
    for secret in ('two secret words', 'hidden-token', 'database-password', 'first', 'second', 'private bytes', '\x1b'):
        assert secret not in result
    assert 'ERROR DNS failed' in result


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_real_model_request_uses_correct_protocol(manager, model_server, protocol):
    manager.save(config(model_server, protocol))
    snapshot = manager.prepare('owner', 'session', 'ERROR cannot resolve postgres\npassword=hidden-value\ntest-key-only')
    assert 'hidden-value' not in snapshot['text'] and 'test-key-only' not in snapshot['text']
    assert not model_server.calls
    submitted = manager.submit('owner', action='analyze', session_id='session', data={
        'snapshot_id': snapshot['id'], 'consent': True, 'question': '为什么连接失败？'})
    result = finished(manager, 'owner', submitted['id'])
    assert result['status'] == 'done' and 'docker ps' in result['text']
    path, headers, body = model_server.calls[0]
    assert 'hidden-value' not in json.dumps(body) and 'test-key-only' not in json.dumps(body)
    if protocol == 'anthropic':
        assert path == '/v1/messages' and headers['X-Api-Key'] == 'test-key-only'
        assert headers['Anthropic-Version'] == '2023-06-01' and body['max_tokens'] == 2048
        assert body['system'] and all(m['role'] != 'system' for m in body['messages'])
        assert 'Authorization' not in headers
    else:
        assert path == '/v1/chat/completions' and headers['Authorization'] == 'Bearer test-key-only'
        assert body['messages'][0]['role'] == 'system'


@pytest.mark.parametrize('status', [302, 401, 404, 429, 500])
def test_provider_errors_do_not_leak_response_or_follow_redirect(manager, model_server, status):
    model_server.status, model_server.content = status, {'error': 'test-key-only private provider detail'}
    submitted = manager.submit('owner', action='test', data=config(model_server))
    result = finished(manager, 'owner', submitted['id'])
    assert result['status'] == 'failed' and 'test-key-only' not in json.dumps(result)
    assert len(model_server.calls) == 1


def test_requires_consent_matching_owner_session_and_config(manager, model_server):
    manager.save(config(model_server))
    snapshot = manager.prepare('owner', 'session', 'ERROR test')
    with pytest.raises(ai.AIError):
        manager.submit('owner', action='analyze', session_id='session', data={'snapshot_id': snapshot['id']})
    for owner, session in [('other', 'session'), ('owner', 'other')]:
        with pytest.raises(ai.AIError):
            manager.submit(owner, action='analyze', session_id=session, data={'snapshot_id': snapshot['id'], 'consent': True})
    manager.save({**config(model_server), 'model': 'changed'})
    with pytest.raises(ai.AIError, match='重新预览'):
        manager.submit('owner', action='analyze', session_id='session', data={'snapshot_id': snapshot['id'], 'consent': True})
    assert not model_server.calls


def test_job_ownership_concurrency_and_revocation(manager, model_server):
    model_server.gate = threading.Event()
    submitted = manager.submit('owner', action='test', data=config(model_server))
    with pytest.raises(ai.AIError) as error:
        manager.submit('owner', action='test', data=config(model_server))
    assert error.value.status == 409
    with pytest.raises(ai.AIError):
        manager.job('other', submitted['id'])
    manager.revoke('owner')
    with pytest.raises(ai.AIError):
        manager.job('owner', submitted['id'])
    assert manager.jobs[submitted['id']]['status'] == 'cancelling'
    model_server.gate.set()


def test_settings_and_test_work_without_ssh_but_require_login_csrf_origin_and_tls(http_server, manager, model_server, monkeypatch):
    assert request(http_server, 'GET', '/terminal-ai/settings', authenticated=False)[0] == 401
    status, result = request(http_server, 'GET', '/terminal-ai/settings')
    assert status == 200 and not result['configured'] and result['csrf_token']
    assert request(http_server, 'POST', '/terminal-ai/settings', config(model_server), csrf=False)[0] == 403
    assert request(http_server, 'POST', '/terminal-ai/settings', config(model_server), origin=False)[0] == 403
    assert request(http_server, 'POST', '/terminal-ai/settings', config(model_server))[0] == 200
    status, result = request(http_server, 'POST', '/terminal-ai/test', config(model_server, 'anthropic'))
    assert status == 200
    assert finished(manager, ai.owner(http_server.cookie), result['id'])['status'] == 'done'
    monkeypatch.setattr(agent.Handler, '_ssh_secure', lambda self: False)
    assert request(http_server, 'POST', '/terminal-ai/settings', config(model_server))[0] == 403
    assert request(http_server, 'POST', '/terminal-ai/test', config(model_server))[0] == 403
    assert request(http_server, 'GET', '/terminal-ai/settings')[0] == 200


def test_prepare_uses_owner_checked_ssh_output_without_consuming_reader(http_server, manager, model_server, monkeypatch):
    manager.save(config(model_server))
    seen = []
    session = SimpleNamespace(condition=threading.Condition(), chunks=[(1, b'ERROR DNS failed\npassword=private')])
    def owned(owner, key):
        seen.append((owner, key))
        if key != 'allowed':
            raise ai.AIError('not owned', 404)
        return session
    monkeypatch.setattr(agent, '_ssh_manager', lambda: SimpleNamespace(session=owned))
    assert request(http_server, 'POST', '/terminal-ai/prepare', {'session_id': 'other'})[0] == 404
    status, result = request(http_server, 'POST', '/terminal-ai/prepare', {'session_id': 'allowed'})
    assert status == 200 and 'DNS failed' in result['text'] and 'private' not in result['text']
    assert len(session.chunks) == 1 and not model_server.calls
    assert seen[-1][0] == ai.owner(http_server.cookie)


def test_installer_publishes_ai_backend_and_real_ui_assets():
    root = agent.UI_DIR.parent
    assert 'terminal_ai.py' in (root / 'install.sh').read_text(encoding='utf-8')
    assert (agent.UI_DIR / 'terminal-ai.js').exists()
    assert 'terminal-ai.js' in agent.UI_ASSET_TYPES


def chat_data(manager, question='为什么连接失败？', **kwargs):
    return {'consent': True, 'provider': manager.settings(), 'question': question, **kwargs}


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_chat_followup_retains_sanitized_context_and_new_chat_is_independent(manager, model_server, protocol):
    manager.save(config(model_server, protocol))
    first = manager.submit('owner', action='chat', session_id='ssh', output='ERROR DNS failed\npassword=secret-value',
                           data=chat_data(manager, 'test-key-only 连接失败怎么办？'))
    result = finished(manager, 'owner', first['id'])
    assert result['status'] == 'done' and result['context']['lines'] == 2
    assert not any(k.startswith('_') for k in result)
    second = manager.submit('owner', action='chat', session_id='ssh', data=chat_data(
        manager, '你刚才建议的命令是什么意思？', conversation_id=first['conversation_id']))
    assert finished(manager, 'owner', second['id'])['status'] == 'done'
    path, _, payload = model_server.calls[-1]
    messages = [m for m in payload['messages'] if m['role'] != 'system']
    assert [m['role'] for m in messages] == ['user', 'assistant', 'user']
    assert 'DNS failed' in messages[0]['content'] and 'docker ps' in messages[1]['content']
    assert '是什么意思' in messages[-1]['content']
    assert 'secret-value' not in json.dumps(payload) and 'test-key-only' not in json.dumps(payload)
    assert path.endswith('/messages' if protocol == 'anthropic' else '/chat/completions')
    third = manager.submit('owner', action='chat', data=chat_data(manager, '什么是 Docker？'))
    assert finished(manager, 'owner', third['id'])['context']['characters'] == 0
    assert third['conversation_id'] != first['conversation_id']
    messages = [m for m in model_server.calls[-1][2]['messages'] if m['role'] != 'system']
    assert len(messages) == 1 and 'DNS failed' not in messages[0]['content']


def test_chat_rejects_invalid_question_consent_destination_and_foreign_scope(manager, model_server):
    manager.save(config(model_server))
    for values in [{'consent': False}, {'provider': {}}, {'question': ''}, {'question': ' '},
                   {'question': 42}, {'question': 'x' * 2001}, {'conversation_id': 'unknown'}]:
        with pytest.raises(ai.AIError):
            manager.submit('owner', action='chat', data=chat_data(manager, **values))
    assert not model_server.calls
    first = manager.submit('owner', action='chat', session_id='ssh', data=chat_data(manager))
    finished(manager, 'owner', first['id'])
    followup = chat_data(manager, conversation_id=first['conversation_id'])
    with pytest.raises(ai.AIError) as error:
        manager.submit('other', action='chat', session_id='ssh', data=followup)
    assert error.value.status == 404
    with pytest.raises(ai.AIError) as error:
        manager.submit('owner', action='chat', session_id='different-ssh', data=followup)
    assert error.value.status == 409
    manager.save({**config(model_server), 'api_key': 'changed-key'})
    with pytest.raises(ai.AIError) as error:
        manager.submit('owner', action='chat', session_id='ssh', data=chat_data(
            manager, conversation_id=first['conversation_id']))
    assert error.value.status == 409
    assert len(model_server.calls) == 1


def test_chat_failures_and_logout_never_enter_history(manager, model_server):
    manager.save(config(model_server))
    model_server.status = 500
    first = manager.submit('owner', action='chat', data=chat_data(manager, '失败的问题'))
    assert finished(manager, 'owner', first['id'])['status'] == 'failed'
    assert not manager.conversations[first['conversation_id']]['messages']
    model_server.status = 200
    model_server.gate = threading.Event()
    second = manager.submit('owner', action='chat', data=chat_data(manager,
                            conversation_id=first['conversation_id']))
    manager.revoke('owner')
    assert not manager.conversations
    model_server.gate.set()
    deadline = time.monotonic() + 4
    while manager.jobs[second['id']]['_active'] and time.monotonic() < deadline:
        time.sleep(.01)
    assert manager.jobs[second['id']]['status'] == 'cancelled'
    assert 'text' not in manager.jobs[second['id']] and '_user_content' not in manager.jobs[second['id']]
    assert not manager.conversations


def test_chat_memory_bounds_eviction_and_expiry(manager, model_server, monkeypatch):
    manager.save(config(model_server))
    seen = []
    monkeypatch.setattr(ai, 'complete', lambda config, messages: seen.append(messages) or {'text': '回答' * 4000, 'usage': {}})
    conversation = ''
    for i in range(66):
        result = manager.submit('owner', action='chat', output='日志\n' * 8000,
                                data=chat_data(manager, str(i), conversation_id=conversation))
        conversation = result['conversation_id']
        assert finished(manager, 'owner', result['id'])['status'] == 'done'
    history = manager.conversations[conversation]
    assert len(history['messages']) % 2 == 0 and len(history['messages']) <= 12
    assert sum(len(m['content']) for m in history['messages']) <= 32000
    assert len(manager.jobs) <= 64
    assert all(len(m['content']) < 18100 for m in seen[-1])
    for i in range(25):
        result = manager.submit('owner', action='chat', data=chat_data(manager, str(i)))
        finished(manager, 'owner', result['id'])
    assert len(manager.conversations) == 24 and conversation not in manager.conversations
    manager.conversations[result['conversation_id']]['updated'] -= 1801
    with pytest.raises(ai.AIError) as error:
        manager.submit('owner', action='chat', data=chat_data(manager, conversation_id=result['conversation_id']))
    assert error.value.status == 404


def test_chat_route_checks_access_and_optional_owned_ssh_output(http_server, manager, model_server, monkeypatch):
    manager.save(config(model_server))
    data = chat_data(manager)
    assert request(http_server, 'POST', '/terminal-ai/chat', data, authenticated=False)[0] == 401
    assert request(http_server, 'POST', '/terminal-ai/chat', data, csrf=False)[0] == 403
    assert request(http_server, 'POST', '/terminal-ai/chat', data, origin=False)[0] == 403
    seen = []
    session = SimpleNamespace(condition=threading.Condition(), chunks=[(1, b'ERROR DNS failed\npassword=private')])
    def owned(owner, key):
        seen.append((owner, key))
        if key != 'allowed':
            raise ai.AIError('not owned', 404)
        return session
    monkeypatch.setattr(agent, '_ssh_manager', lambda: SimpleNamespace(session=owned))
    assert request(http_server, 'POST', '/terminal-ai/chat', {**data, 'include_terminal': True})[0] == 400
    assert request(http_server, 'POST', '/terminal-ai/chat', {**data, 'session_id': 'other'})[0] == 404
    status, result = request(http_server, 'POST', '/terminal-ai/chat', data)
    assert status == 200
    finished(manager, ai.owner(http_server.cookie), result['id'])
    assert len(seen) == 1
    assert 'DNS failed' not in json.dumps(model_server.calls[-1][2])
    for include in [True, False]:
        status, result = request(http_server, 'POST', '/terminal-ai/chat', {
            **data, 'session_id': 'allowed', 'include_terminal': include})
        assert status == 200
        done = finished(manager, ai.owner(http_server.cookie), result['id'])
        assert (done['context']['characters'] > 0) is include
        payload = json.dumps(model_server.calls[-1][2])
        assert ('DNS failed' in payload) is include and 'private' not in payload
    assert seen[-1][0] == ai.owner(http_server.cookie) and len(session.chunks) == 1
    monkeypatch.setattr(agent.Handler, '_ssh_secure', lambda self: False)
    assert request(http_server, 'POST', '/terminal-ai/chat', data)[0] == 403
