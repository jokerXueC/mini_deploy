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
    assert manager.jobs[submitted['id']]['status'] == 'running'
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
