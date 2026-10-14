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
    def run(command, **kwargs):
        if command[1:3] == ['image', 'ls']:
            return 0, '\n'.join(map(json.dumps, rows))
        if command[2] == 'ls':
            return 0, CONTAINER
        return 0, IMAGE
    monkeypatch.setattr(agent, '_run_command', run)
    images = agent._docker_images()
    assert len(images) == 2
    assert images[0]['tags'] == ['demo:v1', 'demo:latest']
    assert images[1]['tags'] == []
    assert images[0]['usage'] == 'in_use'
    assert images[1]['usage'] == 'unused'


@pytest.mark.parametrize('failure', ['list', 'inspect', 'malformed'])
def test_image_reference_failure_never_marks_images_unused(monkeypatch, failure):
    def run(command, **kwargs):
        if command[1:3] == ['image', 'ls']:
            return 0, json.dumps(dict(ID=IMAGE, Repository='demo', Tag='v1'))
        if command[2] == 'ls':
            return (1, 'unavailable') if failure == 'list' else (0, CONTAINER)
        return (1, 'gone') if failure == 'inspect' else (0, 'bad-image-id')
    monkeypatch.setattr(agent, '_run_command', run)
    assert agent._docker_images()[0]['usage'] == 'unknown'


@pytest.mark.parametrize('all_unused', [False, True])
def test_cleanup_scope_only_images_and_never_force_removes(monkeypatch, all_unused):
    calls = []
    monkeypatch.setattr(agent, '_run_command', lambda command, **k: (calls.append(command) or 0, 'Total reclaimed space: 12MB'))
    assert '12MB' in agent._docker_image_cleanup(all_unused)
    assert calls == [['docker', 'image', 'prune', '--force', *(['--all'] if all_unused else ['--filter', 'until=24h'])]]


def test_automatic_cleanup_retries_hourly_after_failure_and_stops(monkeypatch):
    class Stop:
        def __init__(self):
            self.waits = []

        def wait(self, seconds):
            self.waits.append(seconds)
            return len(self.waits) == 3

        def is_set(self):
            return False

    calls = []
    def cleanup(all_unused=False):
        calls.append(all_unused)
        if len(calls) == 1:
            raise RuntimeError('daemon temporarily unavailable')
        return 'Total reclaimed space: 0B'
    monkeypatch.setattr(agent, 'DOCKER_AUTO_IMAGE_CLEANUP', True)
    monkeypatch.setattr(agent.shutil, 'which', lambda _: 'docker')
    monkeypatch.setattr(agent, '_docker_image_cleanup', cleanup)
    monkeypatch.setattr(agent, '_docker_image_cleanup_status', {})
    monkeypatch.setattr(agent, '_audit_event', lambda *a, **k: None)
    monkeypatch.setattr(agent, '_log', lambda *a: None)
    stop = Stop()
    agent._docker_image_cleanup_sampler(stop)
    assert calls == [False, False]
    assert stop.waits == [60, 3600, 3600]
    assert agent._docker_image_cleanup_status['ok'] is True


def test_disabled_automatic_cleanup_runs_no_docker_command(monkeypatch):
    monkeypatch.setattr(agent, 'DOCKER_AUTO_IMAGE_CLEANUP', False)
    monkeypatch.setattr(agent, '_docker_image_cleanup', lambda *a, **k: pytest.fail('disabled'))
    agent._docker_image_cleanup_sampler(threading.Event())


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
    cleanups = []
    monkeypatch.setattr(agent, '_docker_image_action', lambda *a: calls.append(a) or 'ok')
    monkeypatch.setattr(agent, '_docker_image_cleanup', lambda **kwargs: cleanups.append(kwargs) or 'reclaimed')
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
        payload = {'action': 'prune_unused'}
        assert request('POST', '/docker/images/action', payload) == 401
        assert request('POST', '/docker/images/action', payload, {'Cookie': headers['Cookie']}) == 403
        assert request('POST', '/docker/images/action', payload, headers) == 400
        assert not cleanups
        payload['confirmed'] = True
        assert request('POST', '/docker/images/action', payload, headers) == 200
        assert cleanups == [{'all_unused': True}]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
