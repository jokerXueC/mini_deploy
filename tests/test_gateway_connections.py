import json

import pytest

import gateway_connections as connections
import request_gateway as gateway
from certificates import CertificateError, atomic_write


CADDY = '''{
    email {$ACME_EMAIL}
    admin off
}

meetpeak.tech {
    encode zstd gzip
    header {
        X-Frame-Options DENY
    }
    handle_path /downloads/* {
        root * /srv/downloads
        file_server
    }
    handle {
        root * /srv/site
        @private path /README.md /.*
        respond @private 404
        file_server
    }
}

aimore.meetpeak.tech {
    header X-Content-Type-Options nosniff
    request_body {
        max_size 32MB
    }
    handle /cloud/* {
        reverse_proxy aimore-cloud:8766
    }
    handle {
        respond "AimOre API {health}" 200
    }
}
'''

NGINX = '''server {
    listen 443 ssl;
    server_name api.example.test;
    location /api/ {
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_pass http://127.0.0.1:8000;
    }
}
'''


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = tmp_path / 'Caddyfile'
    config.write_text(CADDY, encoding='utf-8')
    source = dict(kind='caddy', mode='docker', container='edge', container_id='a' * 64,
                  config_path='/etc/caddy/Caddyfile', path=str(config), networks=['app_default'])
    monkeypatch.setattr(connections, 'resolve', lambda raw: dict(source))
    monkeypatch.setattr(connections, 'command', lambda *_a, **_kw: '[{"Driver":"bridge"}]')
    store = connections.Connections(tmp_path / 'data')
    monkeypatch.setattr(store.gateways, 'inspect', lambda _: None)
    monkeypatch.setattr(store.gateways, 'start', lambda _: None)
    monkeypatch.setattr(store, 'validate', lambda *_: None)
    monkeypatch.setattr(store, 'activate', lambda *_: None)
    monkeypatch.setattr(store, 'probe', lambda *_: None)
    return store, source, config


def preview(store, source, kind='proxy', key='api', **extra):
    inspected = store.inspect(source)
    route = next(r for r in inspected['routes'] if r['kind'] == kind)
    return store.preview({'source': source, 'source_revision': inspected['revision'], 'route_id': route['id'],
                          'key': key, 'port': 18080 if key == 'api' else 18082, 'network': 'app_default', **extra})


def test_caddy_parser_preserves_placeholders_strings_and_static_routes():
    routes = connections.routes(CADDY, 'caddy')
    assert len(routes) == 2
    assert all(r['supported'] for r in routes)
    assert routes[0]['site'] == 'meetpeak.tech'
    assert routes[0]['kind'] == 'static'
    assert routes[1]['upstream'] == 'http://aimore-cloud:8766'
    assert '/cloud/*' in routes[1]['label']


def test_nginx_parser_finds_only_simple_http_upstream():
    route = connections.routes(NGINX, 'nginx')[0]
    assert route['supported'] and route['site'] == 'api.example.test'
    assert route['upstream'] == 'http://127.0.0.1:8000'
    variable = NGINX.replace('http://127.0.0.1:8000', 'http://$backend')
    assert not connections.routes(variable, 'nginx')[0]['supported']


@pytest.mark.parametrize('text', [
    'site.test {\n import shared\n reverse_proxy api:80\n}\n',
    'site.test {\n reverse_proxy api:80 other:80\n}\n',
    'site.test {\n reverse_proxy api:80 {\n header_up Host other\n }\n}\n',
    'site.test {\n tls cert.pem key.pem\n file_server\n}\n',
    'site.test {\n reverse_proxy https://api:443\n}\n',
    'site.test {\n reverse_proxy mini-gateway-old:10000\n}\n',
    'site.test {\n @private remote_ip 127.0.0.1\n respond @private 200\n file_server\n}\n',
    'site.test {\n header Location "{scheme}://site.test/"\n file_server\n}\n',
    '{\n default_bind 127.0.0.1\n}\nsite.test {\n file_server\n}\n',
])
def test_advanced_and_already_connected_routes_are_not_rewritten(text):
    assert not connections.routes(text, 'caddy')[0]['supported']


def test_nginx_inherited_headers_require_manual_review():
    config = NGINX.replace('proxy_set_header Host $host;', '')
    assert not connections.routes(config, 'nginx')[0]['supported']


def test_preview_is_not_live_mutation_and_hides_config_secrets(setup):
    store, source, path = setup
    plan = preview(store, source)
    assert path.read_text() == CADDY
    assert store.gateways.entries() == []
    assert 'ACME_EMAIL' not in json.dumps(plan)
    assert plan['gateway']['network'] == 'app_default'
    assert plan['gateway']['trust_proxy'] is True
    assert 'mini-gateway-api:10000' in plan['after_rule']


def test_network_must_belong_to_front_proxy(setup):
    store, source, _ = setup
    with pytest.raises(CertificateError, match='已加入'):
        preview(store, source, network='host')


def test_stale_source_and_stale_confirmation_are_rejected(setup):
    store, source, path = setup
    plan = preview(store, source)
    with pytest.raises(CertificateError, match='过期'):
        store.apply({'key': 'api', 'token': 'wrong'})
    path.write_text(CADDY + '# user change\n')
    with pytest.raises(CertificateError, match='已变化'):
        store.apply({'key': 'api', 'token': plan['token']})
    assert not store.gateways.entries()


def test_attach_and_detach_preserve_inode_and_other_sites(setup):
    store, source, path = setup
    inode = path.stat().st_ino
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    assert path.stat().st_ino == inode
    assert 'reverse_proxy mini-gateway-api:10000' in path.read_text()
    assert store.status('api')['state'] == 'connected'
    assert '/cloud/*' in store.status('api')['scope']
    assert (store.directory('api') / 'before.conf').read_text() == CADDY
    with path.open('a', newline='') as stream:
        stream.write('# unrelated change\n')
    store.disconnect({'key': 'api', 'token': plan['token']})
    assert path.read_text() == CADDY + '# unrelated change\n'
    assert store.status('api')['state'] == 'not_connected'
    assert store.gateways.read('api')


def test_two_sites_in_one_caddyfile_can_be_attached_and_detached_independently(setup):
    store, source, path = setup
    api = preview(store, source)
    store.apply({'key': 'api', 'token': api['token']})
    static = preview(store, source, 'static', 'website')
    store.apply({'key': 'website', 'token': static['token']})
    text = path.read_text()
    assert 'reverse_proxy mini-gateway-api:10000' in text
    assert 'reverse_proxy mini-gateway-website:10000' in text
    assert 'http://:18081 {' in text
    assert '/srv/downloads' in text and '/srv/site' in text
    assert static['gateway']['upstream'] == 'http://edge:18081'
    store.disconnect({'key': 'api', 'token': api['token']})
    assert 'reverse_proxy aimore-cloud:8766' in path.read_text()
    assert 'mini-gateway-website:10000' in path.read_text()
    store.disconnect({'key': 'website', 'token': static['token']})
    assert path.read_text() == CADDY


def test_second_connection_on_same_site_is_rejected(setup):
    store, source, _ = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    with pytest.raises(CertificateError):
        preview(store, source, key='duplicate')


def test_apply_failure_restores_config_and_keeps_gateway(setup, monkeypatch):
    store, source, path = setup
    plan = preview(store, source)
    calls = []

    def activate(_):
        calls.append(path.read_text())
        if len(calls) == 1:
            raise CertificateError('restart failed')

    monkeypatch.setattr(store, 'activate', activate)
    with pytest.raises(CertificateError, match='已恢复'):
        store.apply({'key': 'api', 'token': plan['token']})
    assert path.read_text() == CADDY
    assert len(calls) == 2
    assert store.status('api')['state'] == 'not_connected'
    assert store.gateways.read('api')


def test_interrupted_apply_can_be_undone(setup, monkeypatch):
    store, source, path = setup
    plan = preview(store, source)

    def interrupted(_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(store, 'activate', interrupted)
    with pytest.raises(KeyboardInterrupt):
        store.apply({'key': 'api', 'token': plan['token']})
    assert store.status('api')['state'] == 'applying'
    monkeypatch.setattr(store, 'activate', lambda _: None)
    store.disconnect({'key': 'api', 'token': plan['token']})
    assert path.read_text() == CADDY


def test_detach_does_not_overwrite_changed_rule(setup):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    changed = path.read_text().replace('mini-gateway-api:10000', 'custom-upstream:9000')
    path.write_text(changed)
    with pytest.raises(CertificateError, match='外部修改'):
        store.disconnect({'key': 'api', 'token': plan['token']})
    assert path.read_text() == changed


def test_stopping_attached_gateway_is_blocked(setup):
    store, source, _ = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    entry = store.gateways.read('api')
    with pytest.raises(CertificateError, match='先撤销'):
        store.gateways.operate({'action': 'stop', 'key': 'api', 'revision': gateway.revision(entry['spec']), 'confirmed': True})
    with pytest.raises(CertificateError, match='先撤销'):
        store.gateways.save({**entry['spec'], 'upstream': 'http://different:80'}, gateway.revision(entry['spec']))


@pytest.mark.parametrize('slash', ['', '/'])
def test_nginx_attach_and_detach_preserve_headers(setup, slash):
    store, source, path = setup
    source['kind'] = 'nginx'
    source['networks'] = ['host']
    original = NGINX.replace('http://127.0.0.1:8000;', f'http://127.0.0.1:8000{slash};')
    path.write_text(original)
    plan = preview(store, source, network='host')
    store.apply({'key': 'api', 'token': plan['token']})
    assert f'proxy_pass http://127.0.0.1:18080{slash};' in path.read_text()
    assert 'proxy_set_header Host $host;' in path.read_text()
    store.disconnect({'key': 'api', 'token': plan['token']})
    assert path.read_text() == original


def test_failed_disconnect_restores_connected_config(setup, monkeypatch):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    connected = connections.read_config(path)
    calls = []

    def activate(_):
        calls.append(connections.read_config(path))
        if len(calls) == 1:
            raise CertificateError('restart failed')

    monkeypatch.setattr(store, 'activate', activate)
    with pytest.raises(CertificateError, match='已恢复撤销前配置'):
        store.disconnect({'key': 'api', 'token': plan['token']})
    assert connections.read_config(path) == connected
    assert store.status('api')['state'] == 'connected'
    assert len(calls) == 2


def test_connection_confirmation_is_required(setup):
    store, _, _ = setup
    with pytest.raises(CertificateError, match='确认'):
        store.operate({'action': 'apply'})


def test_docker_source_maps_longest_bind_mount_and_rejects_changed_startup(tmp_path, monkeypatch):
    config = tmp_path / 'Caddyfile'
    config.write_text(CADDY)
    item = {'Id': 'a' * 64, 'State': {'Status': 'running'},
            'Config': {'Entrypoint': [], 'Cmd': ['caddy', 'run', '--config', '/etc/caddy/Caddyfile']},
            'Mounts': [{'Type': 'bind', 'Source': str(config), 'Destination': '/etc/caddy/Caddyfile'}],
            'NetworkSettings': {'Networks': {'app_default': {}}}}
    monkeypatch.setattr(connections.nginx_runtime, 'require_local_docker', lambda: None)
    monkeypatch.setattr(connections, 'command', lambda args, **kw: json.dumps([item]) if 'inspect' in args else 'v2')
    raw = dict(kind='caddy', mode='docker', container='edge', config_path='/etc/caddy/Caddyfile')
    assert connections.resolve(raw)['path'] == str(config)
    item['Config']['Cmd'].append('--resume')
    with pytest.raises(CertificateError, match='resume'):
        connections.resolve(raw)


def test_static_port_and_probe_injection_are_rejected(setup):
    store, source, _ = setup
    with pytest.raises(CertificateError, match='内部端口'):
        preview(store, source, 'static', internal_port=6868)
    with pytest.raises(CertificateError, match='检测路径'):
        preview(store, source, probe_path='/ok\r\nX-Evil: yes')


def test_private_record_size_bound(tmp_path):
    path = tmp_path / 'record.json'
    atomic_write(path, 'x' * (2 * 1024 * 1024 + 1))
    with pytest.raises(CertificateError, match='异常'):
        connections.read_record(path)


def test_local_caddy_requires_matching_service_config_and_no_unknown_environment(monkeypatch):
    monkeypatch.setattr(connections.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(connections, 'command', lambda args, **kw: '--config /etc/caddy/Caddyfile' if 'show' in args else 'active')
    monkeypatch.setattr(connections, 'read_config', lambda path: 'site.test {\nfile_server\n}\n')
    source = connections.resolve({'kind': 'caddy', 'mode': 'local'})
    assert source['networks'] == ['host']
    assert source['path'] == '/etc/caddy/Caddyfile'
    with pytest.raises(CertificateError, match='启动参数'):
        connections.resolve({'kind': 'caddy', 'mode': 'local', 'config_path': '/etc/caddy/wrong.conf'})
    monkeypatch.setattr(connections, 'read_config', lambda path: CADDY)
    with pytest.raises(CertificateError, match='环境变量'):
        connections.resolve({'kind': 'caddy', 'mode': 'local'})
