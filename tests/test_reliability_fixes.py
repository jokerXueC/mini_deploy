"""Regression cases for bounded collection, probing and inbound connections."""
import http.client
import io
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import agent
import http_limits
import monitoring
import request_history as history
from test_request_history import NOW, line


def fake_docker(monkeypatch, logs):
    class Process:
        def __init__(self, command, **kwargs):
            since = int(command[command.index('--since') + 1])
            until = int(command[command.index('--until') + 1])
            self.stdout = io.BytesIO(('\n'.join(value for value in logs if since <= history.log_second(value) < until) + '\n').encode())

        def poll(self):
            return 0

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(history.subprocess, 'Popen', Process)


def test_high_volume_same_timestamp_resumes_after_restart_without_loss(tmp_path, monkeypatch):
    logs = [line('/same')] * 6000 + [line('/next', ts=NOW - 5)]
    fake_docker(monkeypatch, logs)
    entry = {'spec': {'key': 'api', 'name': 'API'}}
    gateway = SimpleNamespace(read=lambda _: entry, inspect=lambda _: {'Id': 'container-a'}, upgrade_logging=lambda *_: None)
    store = history.History(tmp_path, max_rows=30)
    history.Collector(store).collect(gateway, entry, NOW)
    assert store.source('api')['cursor'] == NOW - 10
    assert store.query('api', now=NOW)['total_requests'] == 5000
    store = history.History(tmp_path, max_rows=30)
    history.Collector(store).collect(gateway, entry, NOW)
    result = store.query('api', now=NOW)
    assert result['total_requests'] == 6001 and result['retained_requests'] == 30
    assert not result['pending'] and result['aggregate_mode']
    history.Collector(store).collect(gateway, entry, NOW + 5)
    assert store.query('api', now=NOW + 5)['total_requests'] == 6001


def test_rotated_resume_anchor_is_not_silently_skipped(tmp_path, monkeypatch):
    logs = [line('/same')] * 6000
    fake_docker(monkeypatch, logs)
    first, identities, checkpoint = history.read_log_batch('container', NOW - 20, NOW - 1)
    assert len(first) == 5000
    logs[0] = line('/changed')
    with pytest.raises(ValueError, match='轮转'):
        history.read_log_batch('container', checkpoint['second'], NOW - 1, checkpoint)


def test_existing_raw_history_backfills_once_without_erasing_data(tmp_path):
    store = history.History(tmp_path)
    store.ingest('api', 'API', 'a', [line('/old', ts=NOW - 500)], NOW - 1, now=NOW)
    with store.connect() as db:
        db.execute('DROP TABLE request_rollups')
        db.execute('DROP TABLE request_schema')
        db.execute('UPDATE sources SET rollups_since=0')
    restored = history.History(tmp_path, max_rows=1)
    restored.ingest('api', 'API', 'a', [line('/new')], NOW - 1, now=NOW)
    assert restored.query('api', now=NOW)['total_requests'] == 2
    assert history.History(tmp_path).query('api', now=NOW)['total_requests'] == 2


def test_routine_events_do_not_evict_incidents_across_restart(tmp_path):
    monitor = monitoring.Monitor(tmp_path / 'monitor.json', clock=lambda: NOW)
    monitor.record_event('alert', 'service down', '', 'website')
    monitor.record_event('recovery', 'service restored', '', 'website')
    for _ in range(1100):
        monitor.record_event('lifecycle', 'container start', '', 'docker')
    monitor.flush()
    restored = monitoring.Monitor(monitor.path, clock=lambda: NOW)
    assert len(restored.events) == 1002
    assert [item['kind'] for item in restored.events[:2]] == ['alert', 'recovery']


def test_probe_secrets_preserved_masked_and_explicitly_clearable(tmp_path):
    monitor = monitoring.Monitor(tmp_path / 'monitor.json')
    result = monitor.operation({'action': 'save', 'url': 'http://localhost/health', 'probe': {
        'method': 'POST', 'headers': {'Authorization': 'secret-value'}, 'body': 'private-body',
        'assertion': {'kind': 'json', 'path': 'ready', 'value': True}}})
    assert 'secret-value' not in json.dumps(result) and 'private-body' not in json.dumps(result)
    key = result['targets'][0]['key']
    monitor.operation({'action': 'save', 'key': key, 'url': 'http://localhost/health',
                       'probe': {'method': 'POST', 'headers': None, 'body': None}})
    assert monitor.targets[key]['probe']['headers']['Authorization'] == 'secret-value'
    monitor.operation({'action': 'save', 'key': key, 'url': 'http://localhost/health', 'probe': {'headers': {}, 'body': ''}})
    assert not monitor.targets[key]['probe']['headers']
    assert 'secret-value' not in monitor.path.read_text()


def test_authenticated_post_and_business_failure_are_checked_without_body_leak():
    received = []

    class Backend(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append((self.headers.get('Authorization'), self.rfile.read(int(self.headers.get('Content-Length', 0)))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":{"ready":false},"secret":"never-publish"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        target = {'url': f'http://127.0.0.1:{server.server_port}/health', 'probe': {
            'method': 'POST', 'headers': {'Authorization': 'Bearer test'}, 'body': '{}', 'expected_status': [200],
            'assertion': {'kind': 'json', 'path': 'data.ready', 'value': True}}}
        result = monitoring.probe(target, False)
        assert result['code'] == 200 and result['status'] == 'failed'
        assert 'never-publish' not in json.dumps(result)
        assert received == [('Bearer test', b'{}')]
        target['probe']['assertion']['value'] = False
        assert monitoring.probe(target, False)['status'] == 'healthy'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


@pytest.mark.parametrize('probe', [
    {'headers': {'Authorization': 'token\r\nInjected: x'}},
    {'method': 'DELETE'}, {'method': 'HEAD', 'assertion': {'kind': 'contains', 'value': 'ok'}},
    {'headers': {'Content-Length': '999'}}, {'expected_status': [True]},
])
def test_invalid_probe_configuration_is_rejected(probe):
    with pytest.raises(ValueError):
        monitoring.probe_options({'probe': probe})


def test_passive_errors_require_volume_and_missing_data_cannot_recover(tmp_path):
    now = [NOW]
    store = history.History(tmp_path)
    monitor = monitoring.Monitor(tmp_path / 'monitor.json', clock=lambda: now[0])
    store.ingest('api', 'API', 'a', [line('/api', ts=NOW - 400), *[line('/api', ts=NOW - 100, status=504) for _ in range(30)]], NOW - 1, now=NOW)
    store.evaluate(monitor, NOW)
    assert not monitor.snapshot()['alerts']
    now[0] += 60
    store.ingest('api', 'API', 'a', [], now[0] - 1, now=now[0])
    store.evaluate(monitor, now[0])
    assert len(monitor.snapshot()['alerts']) == 2
    store.failure('api', 'API', 'collection unavailable')
    store.evaluate(monitor, now[0])
    assert all(item['stale'] for item in monitor.snapshot()['alerts'])


def test_status_reads_do_not_block_on_docker_sampling(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(agent, '_system_status_cache', {})

    def slow():
        entered.set()
        release.wait(3)
        return {'available': True, 'containers': []}

    monkeypatch.setattr(agent, '_docker_status', slow)
    monkeypatch.setattr(agent, '_record_system_metric', lambda *_: [])
    monkeypatch.setattr(agent, '_performance_history', lambda: SimpleNamespace(record=lambda *_: None))
    worker = threading.Thread(target=agent._sample_system_status)
    worker.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        result = agent._system_status_payload()
        assert time.monotonic() - started < .2 and result['docker']['stale']
    finally:
        release.set()
        worker.join(3)
    assert not agent._system_status_payload()['docker']['stale']


def test_input_deadline_is_absolute_and_does_not_renew_for_drips(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(http_limits.time, 'monotonic', lambda: now[0])
    timeouts = []
    connection = SimpleNamespace(settimeout=timeouts.append, recv_into=lambda b: 1)
    reader = http_limits.DeadlineInput(connection, seconds=10)
    assert reader.readinto(bytearray(1)) == 1
    now[0] += 9
    assert reader.readinto(bytearray(1)) == 1
    assert timeouts == [10, 1]
    now[0] += 2
    with pytest.raises(TimeoutError):
        reader.readinto(bytearray(1))


@pytest.mark.parametrize('url', ['https://second.test/health', 'http://first.test/health', 'https://first.test:8443/health'])
def test_probe_does_not_forward_saved_credentials_to_changed_origin(tmp_path, url):
    monitor = monitoring.Monitor(tmp_path / 'monitor.json')
    target = monitor.operation({'action': 'save', 'url': 'https://first.test/health',
        'probe': {'headers': {'Authorization': 'secret'}}})['targets'][0]
    monitor.operation({'action': 'save', 'key': target['key'], 'url': url})
    assert not monitor.targets[target['key']]['probe'].get('headers')


def test_connection_budget_recovers_and_does_not_pin_accept_loop():
    class Handler(http_limits.RequestDeadlineMixin, BaseHTTPRequestHandler):
        def handle_one_request(self):
            self.rfile.raw.deadline = time.monotonic() + 2
            return BaseHTTPRequestHandler.handle_one_request(self)

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *args):
            pass

    server = http_limits.BoundedHTTPServer(('127.0.0.1', 0), Handler)
    server.max_connections = server.max_connections_per_ip = 1
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    slow = socket.create_connection(server.server_address, timeout=4)
    slow.sendall(b'GET / HTTP/1.1\r\nX-Drip: ')
    try:
        deadline = time.monotonic() + 1
        while not server._active_connections and time.monotonic() < deadline:
            time.sleep(.01)
        # Rejection precedes parsing; do not race a write with the server close.
        with socket.create_connection(server.server_address, timeout=2) as rejected:
            assert rejected.recv(1024).startswith(b'HTTP/1.1 503 ')
        assert slow.recv(1024) == b''
        deadline = time.monotonic() + 1
        while server._active_connections and time.monotonic() < deadline:
            time.sleep(.01)
        client = http.client.HTTPConnection(*server.server_address, timeout=2)
        client.request('GET', '/')
        assert client.getresponse().read() == b'ok'
        client.close()
    finally:
        slow.close()
        server.shutdown()
        server.server_close()
        worker.join(3)
