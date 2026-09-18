import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

import agent

CONTAINER = 'a' * 64
IMAGE = 'sha256:' + 'b' * 64


def test_remove_stopped_container_by_immutable_id_without_volumes(monkeypatch):
    calls = []
    monkeypatch.setattr(agent.shutil, 'which', lambda _: 'docker')

    def run(command, **kwargs):
        calls.append(command)
        return (0, f'"{CONTAINER}" "exited"') if command[1] == 'inspect' else (0, CONTAINER)

    monkeypatch.setattr(agent, '_run_command', run)
    agent._docker_action('web', 'remove', CONTAINER)
    assert calls[-1] == ['docker', 'rm', CONTAINER]


@pytest.mark.parametrize('state', ['running', 'paused', 'restarting', 'removing'])
def test_remove_requires_stopped_container(monkeypatch, state):
    monkeypatch.setattr(agent.shutil, 'which', lambda _: 'docker')
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return 0, f'"{CONTAINER}" "{state}"'

    monkeypatch.setattr(agent, '_run_command', run)
    with pytest.raises(ValueError, match='先停止'):
        agent._docker_action('web', 'remove', CONTAINER)
    assert len(calls) == 1


def test_replaced_container_is_never_deleted(monkeypatch):
    monkeypatch.setattr(agent.shutil, 'which', lambda _: 'docker')
    monkeypatch.setattr(agent, '_run_command', lambda *a, **k: (0, f'"{"c" * 64}" "exited"'))
    with pytest.raises(ValueError, match='发生变化'):
        agent._docker_action('web', 'remove', CONTAINER)
    with pytest.raises(ValueError, match='刷新列表'):
        agent._docker_action('web', 'remove')


def test_image_list_groups_tags_and_retains_untagged_images(monkeypatch):
    rows = [dict(ID=IMAGE, Repository='demo', Tag=tag, Size='12MB', CreatedAt='today') for tag in ('v1', 'latest')]
    rows.append(dict(ID='sha256:' + 'c' * 64, Repository='<none>', Tag='<none>', Size='2MB'))
    monkeypatch.setattr(agent, '_run_command', lambda *a, **k: (0, '\n'.join(map(json.dumps, rows))))
    images = agent._docker_images()
    assert len(images) == 2
    assert images[0]['tags'] == ['demo:v1', 'demo:latest']
    assert images[1]['tags'] == []


@pytest.mark.parametrize('reference', ['--all-tags', 'nginx;id', 'nginx\nredis', 'https://host/image', ''])
def test_pull_rejects_shell_or_option_input(monkeypatch, reference):
    monkeypatch.setattr(agent, '_run_command', lambda *a, **k: pytest.fail('must not run'))
    with pytest.raises(ValueError):
        agent._docker_image_action('pull', reference)


def test_image_delete_does_not_force_or_prune(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return 1, 'image is being used by stopped container'

    monkeypatch.setattr(agent, '_run_command', run)
    with pytest.raises(RuntimeError, match='不会强制删除'):
        agent._docker_image_action('remove', IMAGE)
    assert calls == [['docker', 'image', 'rm', IMAGE]]
    with pytest.raises(ValueError):
        agent._docker_image_action('remove', 'nginx:latest')


def test_registry_pull_is_argument_based(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, '_run_command', lambda command, **k: (calls.append(command) or 0, 'ok'))
    agent._docker_image_action('pull', 'registry.example.com:5000/team/app:v1')
    assert calls == [['docker', 'pull', 'registry.example.com:5000/team/app:v1']]


def test_image_routes_require_auth_csrf_and_confirmation(monkeypatch):
    monkeypatch.setattr(agent, 'UI_SESSION_SECRET', 'docker-test-session')
    monkeypatch.setattr(agent, '_audit_event', lambda *a, **k: None)
    monkeypatch.setattr(agent, '_docker_images', lambda: [])
    calls = []
    monkeypatch.setattr(agent, '_docker_image_action', lambda *a: calls.append(a) or 'ok')
    cookie = agent._make_session_cookie()
    headers = {'Cookie': f'{agent.COOKIE_NAME}={cookie}'}
    server = ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)

    def request(method, path, payload=None, auth=None):
        connection.request(method, path, body=json.dumps(payload) if payload else None,
                           headers={'Content-Type': 'application/json', **(auth or {})})
        response = connection.getresponse()
        response.read()
        return response.status

    try:
        assert request('GET', '/docker/images') == 401
        assert request('GET', '/docker/images', auth=headers) == 200
        payload = {'action': 'remove', 'reference': IMAGE}
        assert request('POST', '/docker/images/action', payload) == 401
        assert request('POST', '/docker/images/action', payload, headers) == 403
        headers['X-CSRF-Token'] = agent._csrf_token(cookie)
        assert request('POST', '/docker/images/action', payload, headers) == 400
        assert not calls
        payload['confirmed'] = True
        assert request('POST', '/docker/images/action', payload, headers) == 200
        assert calls == [('remove', IMAGE)]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
