"""Opt-in native Caddy/Nginx static-site insertion, with real HTTP requests."""
import http.client
import os
import socket
import subprocess
import time
import uuid

import pytest

import gateway_connections as connections
import request_gateway as gateway


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


@pytest.mark.skipif(not (os.environ.get('MINI_DEPLOY_TEST_CADDY') and os.environ.get('MINI_DEPLOY_TEST_NGINX')),
                    reason='Set paths to native Caddy and Nginx executables')
def test_real_static_website_attach_request_and_detach(tmp_path, monkeypatch):
    caddy = os.environ['MINI_DEPLOY_TEST_CADDY']
    nginx = os.environ['MINI_DEPLOY_TEST_NGINX']
    front_port, gateway_port, internal_port = free_port(), free_port(), free_port()
    site = tmp_path / 'site'
    site.mkdir()
    (site / 'index.html').write_text('original-static-content', encoding='utf-8')
    config = tmp_path / 'Caddyfile'
    original = ('{\n    admin off\n}\n' + f'http://127.0.0.1:{front_port} {{\n'
                + f'    root * "{site.as_posix()}"\n    file_server\n}}\n')
    config.write_text(original, encoding='utf-8')
    store = connections.Connections(tmp_path / 'data')
    source = dict(kind='caddy', mode='docker', container='edge', container_id='a' * 64,
                  config_path='/etc/caddy/Caddyfile', path=str(config), networks=['host'])
    monkeypatch.setattr(connections, 'resolve', lambda raw: dict(source))
    monkeypatch.setattr(store.gateways, 'inspect', lambda _: None)
    caddy_process = None
    nginx_process = None
    log = (tmp_path / 'processes.log').open('ab')
    nginx_root = tmp_path / 'nginx'
    nginx_root.mkdir()
    (nginx_root / 'logs').mkdir()
    nginx_file = nginx_root / 'nginx.conf'
    nginx_args = [nginx, '-p', nginx_root.as_posix() + '/', '-c', str(nginx_file)]

    def validate(_source, content):
        candidate = tmp_path / 'candidate.Caddyfile'
        candidate.write_text(content, encoding='utf-8')
        subprocess.run([caddy, 'validate', '--config', str(candidate), '--adapter', 'caddyfile'],
                       check=True, stdout=log, stderr=log, timeout=15)

    def activate(_source):
        nonlocal caddy_process
        if caddy_process is not None:
            caddy_process.terminate()
            caddy_process.wait(timeout=10)
        caddy_process = subprocess.Popen([caddy, 'run', '--config', str(config), '--adapter', 'caddyfile'],
                                         stdout=log, stderr=log)
        for attempt in range(30):
            try:
                with socket.create_connection(('127.0.0.1', front_port), timeout=1):
                    return
            except OSError:
                time.sleep(0.1)
        pytest.fail('Caddy did not start')

    def start(entry):
        nonlocal nginx_process
        root = nginx_root.as_posix()
        text = gateway.config(entry['spec']).replace('/dev/stdout', f'{root}/access.log')
        text = text.replace('/dev/stderr', f'{root}/error.log').replace('/tmp/', f'{root}/')
        text = text.replace('worker_connections 2048', 'worker_connections 512')
        nginx_file.write_text(text, encoding='utf-8')
        subprocess.run([*nginx_args, '-t'], check=True, stdout=log, stderr=log, timeout=10)
        nginx_process = subprocess.Popen([*nginx_args, '-g', 'daemon off;'], cwd=nginx_root, stdout=log, stderr=log)
        gateway.Store.wait_listener(gateway_port)

    def visit():
        client = http.client.HTTPConnection('127.0.0.1', front_port, timeout=5)
        try:
            client.request('GET', '/')
            response = client.getresponse()
            assert response.status == 200
            assert response.read() == b'original-static-content'
        finally:
            client.close()

    monkeypatch.setattr(store, 'validate', validate)
    monkeypatch.setattr(store, 'activate', activate)
    monkeypatch.setattr(store.gateways, 'start', start)
    try:
        activate(source)
        visit()
        inspection = store.inspect(source)
        plan = store.preview({'source': source, 'source_revision': inspection['revision'],
                              'route_id': inspection['routes'][0]['id'], 'key': 'website',
                              'network': 'host', 'port': gateway_port, 'internal_port': internal_port})
        store.apply({'key': 'website', 'token': plan['token']})
        visit()
        for _ in range(30):
            if 'mini_deploy_req ' in (nginx_root / 'access.log').read_text():
                break
            time.sleep(0.1)
        assert 'mini_deploy_req ' in (nginx_root / 'access.log').read_text()
        store.disconnect({'key': 'website', 'token': plan['token']})
        assert config.read_text() == original
        visit()
    finally:
        if caddy_process is not None:
            caddy_process.terminate()
            caddy_process.wait(timeout=10)
        if nginx_process is not None:
            subprocess.run([*nginx_args, '-s', 'stop'], stdout=log, stderr=log, timeout=10)
            try:
                nginx_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                nginx_process.kill()
                nginx_process.wait(timeout=5)
        log.close()


@pytest.mark.skipif(os.environ.get('MINI_DEPLOY_GATEWAY_DOCKER_TESTS') != '1', reason='Linux Docker opt-in')
def test_real_docker_caddy_static_site_connection(tmp_path):
    token = uuid.uuid4().hex[:10]
    network, edge_name, key = f'connect-test-{token}', f'edge-{token}', f'web-{token}'
    front_port, gateway_port, internal_port = free_port(), free_port(), free_port()
    site = tmp_path / 'site'
    site.mkdir()
    (site / 'index.html').write_text('docker-static-ok')
    config = tmp_path / 'Caddyfile'
    original = '{\n admin off\n}\nhttp://site.test:8080 {\n root * /srv/site\n file_server\n}\n'
    config.write_text(original)
    source = {'kind': 'caddy', 'mode': 'docker', 'container': edge_name, 'config_path': '/etc/caddy/Caddyfile'}
    store = connections.Connections(tmp_path / 'data')
    gateway.run(['docker', 'pull', 'caddy:2-alpine'], timeout=180)
    gateway.run(['docker', 'network', 'create', network])
    edge_id = ''

    def visit():
        last = None
        for _ in range(20):
            client = http.client.HTTPConnection('127.0.0.1', front_port, timeout=2)
            try:
                client.request('GET', '/', headers={'Host': 'site.test:8080'})
                response = client.getresponse()
                body = response.read()
                if response.status == 200 and body == b'docker-static-ok':
                    return
                last = (response.status, body[:100])
            except (OSError, http.client.HTTPException) as exc:
                last = str(exc)
            finally:
                client.close()
            time.sleep(0.2)
        pytest.fail(f'Front proxy did not serve original website: {last}')

    try:
        edge_id = gateway.run(['docker', 'run', '-d', '--name', edge_name, '--network', network,
                              '-p', f'127.0.0.1:{front_port}:8080',
                              '--mount', f'type=bind,src={config},dst=/etc/caddy/Caddyfile,readonly',
                              '--mount', f'type=bind,src={site},dst=/srv/site,readonly', 'caddy:2-alpine']).strip()
        visit()
        inspection = store.inspect(source)
        plan = store.preview({'source': source, 'source_revision': inspection['revision'],
                              'route_id': inspection['routes'][0]['id'], 'key': key,
                              'network': network, 'port': gateway_port, 'internal_port': internal_port})
        store.operate({'action': 'apply', 'key': key, 'token': plan['token'], 'confirmed': True})
        visit()
        assert store.gateways.records(key)['records']
        store.operate({'action': 'disconnect', 'key': key, 'token': plan['token'], 'confirmed': True})
        assert config.read_text() == original
        visit()
    finally:
        if (store.gateways.directory(key) / 'entry.json').exists():
            item = store.gateways.inspect(store.gateways.read(key))
            if item:
                gateway.run(['docker', 'rm', '-f', item['Id']])
        if edge_id:
            gateway.run(['docker', 'rm', '-f', edge_id])
        gateway.run(['docker', 'network', 'rm', network])
