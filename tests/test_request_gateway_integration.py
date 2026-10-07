"""Opt-in real proxy checks; see docs/REQUEST_GATEWAY.zh-CN.md for invocation."""
import base64
import hashlib
import http.client
import json
import os
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import request_gateway as gateway
from nginx_requests import parse


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


@pytest.fixture
def backend():
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            if self.path == '/events':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(b'data: first\n\n')
                self.wfile.flush()
                release.wait(5)
                self.wfile.write(b'data: last\n\n')
                self.close_connection = True
                return
            if self.path == '/ws':
                key = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                self.send_response(101)
                self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade')
                self.send_header('Sec-WebSocket-Accept', base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
                self.end_headers()
                self.wfile.write(b'\x81\x02ok')
                self.wfile.flush()
                self.close_connection = True
                return
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            data = json.dumps({'path': self.path, 'body': body.decode(), 'host': self.headers.get('Host'),
                               'proto': self.headers.get('X-Forwarded-Proto'),
                               'forwarded': self.headers.get('X-Forwarded-For')}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_POST = do_GET

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(params=['native', 'docker'])
def proxy(request, backend, tmp_path):
    mode = request.param
    binary = os.environ.get('MINI_DEPLOY_TEST_NGINX', '')
    if mode == 'native' and not binary:
        pytest.skip('Set MINI_DEPLOY_TEST_NGINX to a native nginx executable')
    if mode == 'docker' and os.environ.get('MINI_DEPLOY_GATEWAY_DOCKER_TESTS') != '1':
        pytest.skip('Set MINI_DEPLOY_GATEWAY_DOCKER_TESTS=1 on Linux with root and Docker')
    spec = gateway.normalize({'key': 'test-' + uuid.uuid4().hex[:12], 'port': free_port(),
                              'upstream': f'http://127.0.0.1:{backend[0]}', 'network': 'host'})
    if mode == 'docker':
        store = gateway.Store(tmp_path)
        saved = store.save(spec)
        entry = store.read(spec['key'])
        try:
            store.start(entry)
            yield spec, lambda: store.records(spec['key'])['records'], lambda new: store.save(new, saved['revision'])
        finally:
            item = store.inspect(entry)
            if item:
                gateway.run(['docker', 'rm', '-f', item['Id']])
        return

    # Adapt only filesystem/platform directives; preserve the generated HTTP proxy block.
    root = tmp_path.as_posix()
    conf = tmp_path / 'nginx.conf'
    (tmp_path / 'logs').mkdir()
    access = tmp_path / 'access.log'

    def write_config(value):
        text = gateway.config(value).replace('/dev/stdout', f'{root}/access.log')
        text = text.replace('/dev/stderr', f'{root}/error.log').replace('/tmp/', f'{root}/')
        text = text.replace('worker_connections 2048', 'worker_connections 512')
        conf.write_text(text, encoding='utf-8')

    write_config(spec)
    command = [str(Path(binary).resolve()), '-p', root + '/', '-c', str(conf)]
    subprocess.run([*command, '-t'], check=True, capture_output=True, timeout=10)
    output = (tmp_path / 'process.log').open('wb')
    process = subprocess.Popen([*command, '-g', 'daemon off;'], cwd=tmp_path, stdout=output, stderr=output)

    def records():
        return [record for line in access.read_text(encoding='utf-8').splitlines() if (record := parse(line))]

    def update(value):
        write_config(value)
        subprocess.run([*command, '-s', 'reload'], check=True, capture_output=True, timeout=10)

    try:
        gateway.Store.wait_listener(spec['port'])
        yield spec, records, update
    finally:
        subprocess.run([*command, '-s', 'stop'], capture_output=True, timeout=10)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        output.close()


def test_real_proxy_post_stream_websocket_privacy_and_reload(proxy, backend):
    spec, records, update = proxy
    connection = http.client.HTTPConnection('127.0.0.1', spec['port'], timeout=3)
    try:
        connection.request('POST', '/echo/a%2Fb?token=query-secret', body='body-secret', headers={
            'Host': 'api.example.test', 'Cookie': 'cookie-secret', 'Authorization': 'Bearer auth-secret',
            'X-Forwarded-For': 'forged', 'X-Forwarded-Proto': 'https'})
        response = connection.getresponse()
        assert response.status == 200
        result = json.loads(response.read())
        assert result['path'] == '/echo/a%2Fb?token=query-secret'
        assert result['body'] == 'body-secret'
        assert result['host'] == 'api.example.test'
        assert result['proto'] == 'http'
        assert result['forwarded'] != 'forged'
        connection.request('GET', '/events')
        response = connection.getresponse()
        assert response.status == 200
        # Arrival before releasing the backend proves response buffering is disabled.
        assert response.readline() == b'data: first\n'
        backend[1].set()
        assert b'data: last' in response.read()
    finally:
        connection.close()
    with socket.create_connection(('127.0.0.1', spec['port']), timeout=3) as ws:
        ws.sendall(b'GET /ws HTTP/1.1\r\nHost: api.example.test\r\nUpgrade: websocket\r\n'
                   b'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                   b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n')
        with ws.makefile('rb') as stream:
            assert b'101' in stream.readline()
            while stream.readline() != b'\r\n':
                pass
            assert stream.read(4) == b'\x81\x02ok'
    for _ in range(50):
        captured = records()
        if len(captured) >= 3:
            break
        time.sleep(0.05)
    assert {record['status'] for record in captured} >= {200, 101}
    assert not any(secret in json.dumps(captured) for secret in ['query-secret', 'body-secret', 'cookie-secret', 'auth-secret'])
    assert all(record['duration_ms'] >= 0 for record in captured)
    update({**spec, 'upstream': f'http://127.0.0.1:{free_port()}'})
    for _ in range(50):
        connection = http.client.HTTPConnection('127.0.0.1', spec['port'], timeout=3)
        try:
            connection.request('GET', '/unreachable')
            response = connection.getresponse()
            response.read()
            if response.status == 502:
                break
        finally:
            connection.close()
        time.sleep(0.05)
    assert response.status == 502


@pytest.mark.skipif(os.environ.get('MINI_DEPLOY_GATEWAY_DOCKER_TESTS') != '1', reason='Linux Docker opt-in')
def test_docker_bridge_dns_backend_recreation_and_lifecycle(tmp_path):
    token = uuid.uuid4().hex[:10]
    network, backend_name = f'gateway-test-{token}', f'backend-{token}'
    spec = gateway.normalize({'key': 'test-' + token, 'network': network, 'port': free_port(),
                              'upstream': f'http://{backend_name}:8080'})
    backend_config = tmp_path / 'backend.conf'
    backend_config.write_text('events {} http { server { listen 8080; location / { return 200 "backend-ok"; } } }')
    store = gateway.Store(tmp_path)
    saved = store.save(spec)
    entry = store.read(spec['key'])
    gateway.run(['docker', 'network', 'create', network])
    backend_command = ['docker', 'run', '-d', '--name', backend_name, '--network', network,
                       '--mount', f'type=bind,src={backend_config},dst=/etc/nginx/nginx.conf,readonly', gateway.IMAGE]
    try:
        gateway.run(['docker', 'pull', gateway.IMAGE], timeout=180)
        gateway.run(backend_command)
        store.start(entry)
        for generation in range(2):
            if generation:
                gateway.run(['docker', 'rm', '-f', backend_name])
                gateway.run(backend_command)
            for _ in range(30):
                connection = http.client.HTTPConnection('127.0.0.1', spec['port'], timeout=3)
                try:
                    connection.request('GET', '/health')
                    response = connection.getresponse()
                    body = response.read()
                    if response.status == 200:
                        break
                finally:
                    connection.close()
                time.sleep(0.5)
            assert body == b'backend-ok'
        assert store.records(spec['key'])['records']
        data = {'key': spec['key'], 'revision': saved['revision'], 'confirmed': True}
        store.operate({**data, 'action': 'stop'})
        store.operate({**data, 'action': 'start'})
        store.operate({**data, 'action': 'stop'})
        assert store.operate({**data, 'action': 'delete'}) == {'deleted': True}
    finally:
        item = store.inspect(entry)
        if item:
            gateway.run(['docker', 'rm', '-f', item['Id']])
        gateway.run(['docker', 'rm', '-f', backend_name])
        gateway.run(['docker', 'network', 'rm', network])
