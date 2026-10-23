import json
import copy
import os
import subprocess

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

ZHUNKEDA = '''{$PUBLIC_DOMAIN} {
    encode zstd gzip
    route {
        @root path /
        redir @root /zhunkeda{uri} 308
        @webBase path /zhunkeda
        redir @webBase /zhunkeda/?{query} 308
        @apiBase path /ad/push
        redir @apiBase /ad/push/?{query} 308
        @demo path /zhunkeda/demo /zhunkeda/demo/*
        handle @demo {
            respond 404
        }
        @api path /ad/push/*
        handle @api {
            request_body {
                max_size 150MB
            }
            header Cache-Control "no-store"
            reverse_proxy api:8080 {
                flush_interval -1
                transport http {
                    dial_timeout 10s
                    response_header_timeout 160s
                }
            }
        }
        @web path /zhunkeda/*
        handle @web {
            reverse_proxy web:3000 {
                flush_interval -1
            }
        }
        handle {
            respond 404
        }
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


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_caddy_embedded_placeholders_and_proxy_options_are_supported(newline):
    text = ZHUNKEDA.replace('\n', newline)
    nodes = connections.parse(text, 'caddy')
    redirs = [node for node in connections.walk(nodes) if node.words[:1] == ['redir']]
    assert redirs[0].words[2] == '/zhunkeda{uri}'
    assert redirs[1].words[2] == '/zhunkeda/?{query}'
    routes = connections.routes(text, 'caddy', {'{$PUBLIC_DOMAIN}': 'https://new.example.test'})
    assert len(routes) == 2 and all(route['supported'] for route in routes)
    assert [route['upstream'] for route in routes] == ['http://api:8080', 'http://web:3000']
    assert all(route['site'] == 'new.example.test' for route in routes)
    assert '/ad/push/*' in routes[0]['label']
    assert '/zhunkeda/*' in routes[1]['label']
    assert routes[0]['_connect_timeout'] == 10000
    assert routes[1]['_connect_timeout'] == 3000


def test_only_domain_environment_is_exposed_and_missing_domains_are_visible():
    values = connections.caddy_addresses(ZHUNKEDA, [
        'PUBLIC_DOMAIN=new.example.test', 'DATABASE_PASSWORD=private-secret', 'OTHER_HOST=other.test'])
    assert values == {'{$PUBLIC_DOMAIN}': 'new.example.test'}
    assert connections.caddy_addresses(ZHUNKEDA, ['PUBLIC_DOMAIN=site.test bad.test']) == {}
    assert connections.caddy_addresses(ZHUNKEDA, ['PUBLIC_DOMAIN=https://u:password@site.test']) == {}
    missing = connections.routes(ZHUNKEDA, 'caddy')
    assert len(missing) == 2 and all(not route['supported'] for route in missing)
    assert all('域名变量' in route['reason'] for route in missing)
    assert connections.caddy_addresses('{$DOMAIN:default.test} {\n reverse_proxy api:80\n}\n', []) == {
        '{$DOMAIN:default.test}': 'default.test'}
    assert connections.caddy_addresses('{$DOMAIN:default.test} {\n reverse_proxy api:80\n}\n', ['DOMAIN=']) == {
        '{$DOMAIN:default.test}': 'default.test'}


@pytest.mark.parametrize('option', [
    'header_up Host different.test', 'dynamic a {\n name api.test\n }',
    'transport http {\n tls\n }', 'transport http {\n dial_timeout 0\n }',
    'transport http {\n dial_timeout 76s\n }', 'transport http {\n read_timeout 2h\n }',
    'flush_interval unsafe', 'health_uri /health',
])
def test_unsupported_proxy_options_are_listed_but_never_rewritten(option):
    text = 'site.test {\n reverse_proxy api:80 {\n ' + option + '\n }\n}\n'
    route = connections.routes(text, 'caddy')[0]
    assert route['upstream'] == 'http://api:80'
    assert not route['supported'] and route['reason']


def test_same_domain_backends_can_be_connected_and_undone_independently(setup):
    store, source, path = setup
    source['addresses'] = {'{$PUBLIC_DOMAIN}': 'new.example.test'}
    original = ZHUNKEDA.replace('\n', '\r\n')
    with path.open('w', encoding='utf-8', newline='') as stream:
        stream.write(original)

    def plan_for(upstream, key, port):
        inspected = store.inspect(source)
        route = next(route for route in inspected['routes'] if route['upstream'] == upstream)
        return store.preview({'source': source, 'source_revision': inspected['revision'], 'route_id': route['id'],
                              'key': key, 'port': port, 'network': 'app_default'})

    api = plan_for('http://api:8080', 'api', 18080)
    assert api['gateway']['max_body_bytes'] == 0
    assert api['gateway']['connect_timeout_ms'] == 10000
    plan = connections.read_record(store.directory('api') / 'plan.json')
    assert plan['candidate'] == original.replace('reverse_proxy api:8080', 'reverse_proxy mini-gateway-api:10000')
    store.apply({'key': 'api', 'token': api['token']})
    web = plan_for('http://web:3000', 'web', 18082)
    store.apply({'key': 'web', 'token': web['token']})
    expected = original.replace('reverse_proxy api:8080', 'reverse_proxy mini-gateway-api:10000').replace(
        'reverse_proxy web:3000', 'reverse_proxy mini-gateway-web:10000')
    assert connections.read_config(path) == expected
    store.disconnect({'key': 'api', 'token': api['token']})
    assert connections.read_config(path) == original.replace('reverse_proxy web:3000', 'reverse_proxy mini-gateway-web:10000')
    assert store.status('web')['state'] == 'connected'
    store.disconnect({'key': 'web', 'token': web['token']})
    assert connections.read_config(path) == original


def test_failed_second_backend_connection_preserves_first(setup, monkeypatch):
    store, source, path = setup
    source['addresses'] = {'{$PUBLIC_DOMAIN}': 'new.example.test'}
    path.write_text(ZHUNKEDA, encoding='utf-8')
    api = preview(store, source)
    store.apply({'key': 'api', 'token': api['token']})
    first = connections.read_config(path)
    inspected = store.inspect(source)
    route = next(route for route in inspected['routes'] if route['upstream'] == 'http://web:3000')
    web = store.preview({'source': source, 'source_revision': inspected['revision'], 'route_id': route['id'],
                         'key': 'web', 'port': 18082, 'network': 'app_default'})
    calls = []

    def activate(_):
        calls.append(1)
        if len(calls) == 1:
            raise CertificateError('restart failed')

    monkeypatch.setattr(store, 'activate', activate)
    with pytest.raises(CertificateError, match='已恢复'):
        store.apply({'key': 'web', 'token': web['token']})
    assert connections.read_config(path) == first
    assert store.status('api')['state'] == 'connected'
    assert store.status('web')['state'] == 'not_connected'


def test_identical_proxy_fragments_can_be_undone_without_touching_other_routes(setup):
    store, source, path = setup
    text = 'site.test {\n handle /a/* {\n reverse_proxy api:80\n }\n handle /b/* {\n reverse_proxy api:80\n }\n}\n'
    assert all(route['supported'] for route in connections.routes(text, 'caddy'))
    path.write_text(text)
    inspected = store.inspect(source)
    plan = store.preview({'source': source, 'source_revision': inspected['revision'], 'route_id': inspected['routes'][1]['id'],
                          'key': 'api', 'port': 18080, 'network': 'app_default'})
    store.apply({'key': 'api', 'token': plan['token']})
    assert path.read_text().startswith(text.split(' handle /b/*')[0])
    store.disconnect({'key': 'api', 'token': plan['token']})
    assert path.read_text() == text


@pytest.mark.skipif(not os.environ.get('MINI_DEPLOY_TEST_CADDY'), reason='Native Caddy opt-in')
def test_real_caddy_validates_original_and_gateway_candidates(setup):
    store, source, path = setup
    source['addresses'] = {'{$PUBLIC_DOMAIN}': 'new.example.test'}
    path.write_text(ZHUNKEDA, encoding='utf-8')
    environment = {**os.environ, 'PUBLIC_DOMAIN': 'new.example.test'}
    def adapt(config):
        result = subprocess.run([os.environ['MINI_DEPLOY_TEST_CADDY'], 'adapt', '--validate',
                                 '--config', str(config), '--adapter', 'caddyfile'],
                                env=environment, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    original = adapt(path)
    inspected = store.inspect(source)
    for route, key, port in zip(inspected['routes'], ['api', 'web'], [18080, 18082]):
        store.preview({'source': source, 'source_revision': inspected['revision'], 'route_id': route['id'],
                       'key': key, 'port': port, 'network': 'app_default'})
        candidate = connections.read_record(store.directory(key) / 'plan.json')['candidate']
        candidate_path = path.with_name(key + '.Caddyfile')
        candidate_path.write_text(candidate, encoding='utf-8')
        expected = copy.deepcopy(original)

        def replace_address(value):
            if isinstance(value, dict):
                if value.get('dial') == route['upstream'].removeprefix('http://'):
                    value['dial'] = f'mini-gateway-{key}:10000'
                for child in value.values():
                    replace_address(child)
            elif isinstance(value, list):
                for child in value:
                    replace_address(child)

        replace_address(expected)
        assert adapt(candidate_path) == expected


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


@pytest.mark.parametrize('kind', ['proxy', 'static'])
def test_live_status_detects_redeployed_config_without_changing_it(setup, kind):
    store, source, path = setup
    plan = preview(store, source, kind)
    store.apply({'key': 'api', 'token': plan['token']})
    assert store.status('api', verify=True)['config_state'] == 'matched'
    current = path.read_text() + '\n# unrelated deployment change\n'
    path.write_text(current)
    assert store.status('api', verify=True)['config_state'] == 'matched'
    # A business redeployment restores its own Caddyfile, bypassing both gateways.
    path.write_text(CADDY)
    status = store.status('api', verify=True)
    assert status['config_state'] == 'changed' and '绕过' in status['error']
    assert path.read_text() == CADDY
    assert store.status('api')['state'] == 'connected'  # Mutation guards remain in force.


def test_live_status_accepts_proxy_formatting_and_detects_missing_config(setup):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    path.write_text(path.read_text().replace('reverse_proxy mini-gateway-', 'reverse_proxy   mini-gateway-'))
    assert store.status('api', verify=True)['config_state'] == 'matched'
    path.rename(path.with_suffix('.moved'))
    status = store.status('api', verify=True)
    assert status['config_state'] == 'unavailable' and '无法核对' in status['error']
    assert not path.exists()


def test_release_stale_connection_preserves_files_history_and_allows_reconnect(setup, monkeypatch):
    import request_history

    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    history = request_history.History(store.root.parent)
    history.ingest('api', 'API', 'old-container', [], 100, now=101)
    history_bytes = history.path.read_bytes()
    backup = (store.directory('api') / 'before.conf').read_bytes()
    current = CADDY.replace('reverse_proxy aimore-cloud:8766', 'reverse_proxy aimore-cloud:8766 {\n flush_interval -1\n }')
    path.write_text(current)
    before = path.stat()
    monkeypatch.setattr(store, 'activate', lambda *_: pytest.fail('must not restart a live website'))
    result = store.operate({'action': 'release', 'key': 'api', 'token': plan['token'], 'confirmed': True})
    assert result['connection']['state'] == 'not_connected'
    assert path.read_text() == current and path.stat().st_mtime_ns == before.st_mtime_ns
    assert history.path.read_bytes() == history_bytes
    assert (store.directory('api') / 'before.conf').read_bytes() == backup
    assert (store.directory('api') / 'connection.json').exists()
    assert preview(store, source)['gateway']['key'] == 'api'


@pytest.mark.parametrize('suffix', ['', '\nimport extra.caddy\n'])
def test_release_refuses_active_gateway_or_imported_rules(setup, suffix):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    if suffix:
        path.write_text(CADDY + suffix)
    before = path.read_bytes()
    with pytest.raises(CertificateError, match='无法安全解除'):
        store.release({'key': 'api', 'token': plan['token']})
    assert path.read_bytes() == before and store.status('api')['state'] == 'connected'


def test_release_requires_confirmation_and_current_token(setup):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    path.write_text(CADDY)
    with pytest.raises(CertificateError, match='确认'):
        store.operate({'action': 'release', 'key': 'api', 'token': plan['token']})
    with pytest.raises(CertificateError, match='状态已变化'):
        store.release({'key': 'api', 'token': 'wrong'})
    assert store.status('api')['state'] == 'connected'


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
    result = store.disconnect({'key': 'api', 'token': plan['token']})
    assert result['connection']['state'] == 'not_connected'
    assert path.read_text() == changed


def test_disconnect_relocates_recreated_container_and_preserves_old_file(setup):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    attached = path.read_text()
    moved = path.with_name('new-Caddyfile')
    moved.write_text(attached)
    source.update(container_id='b' * 64, path=str(moved))
    assert store.status('api', verify=True)['config_state'] == 'matched'
    store.disconnect({'key': 'api', 'token': plan['token']})
    assert moved.read_text() == CADDY
    assert path.read_text() == attached


def test_repair_existing_binding_updates_runtime_identity_without_reload(setup, monkeypatch):
    store, source, _ = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    source['container_id'] = 'b' * 64
    monkeypatch.setattr(store, 'activate', lambda *_: pytest.fail('unchanged rules need no reload'))
    result = store.operate({'action': 'repair', 'key': 'api', 'token': plan['token'], 'confirmed': True})
    assert result['status'] == 'repaired'
    assert connections.read_record(store.directory('api') / 'connection.json')['source']['container_id'] == 'b' * 64


def test_repair_restores_original_route_after_redeploy_and_retains_history(setup):
    import request_history
    from test_request_history import NOW, line

    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    history = request_history.History(store.root.parent)
    history.ingest('api', 'API', 'container', [line('/old')], NOW - 1, now=NOW)
    current = CADDY.replace('    handle /cloud/* {', '    handle @admin_ui {\n        reverse_proxy aimore-cloud:8766\n    }\n    handle /cloud/* {')
    path.write_text(current)
    source['container_id'] = 'b' * 64
    result = store.operate({'action': 'repair', 'key': 'api', 'token': plan['token'], 'confirmed': True})
    assert result['status'] == 'repaired' and result['key'] == 'api'
    restored = connections.routes(path.read_text(), 'caddy')
    assert next(r for r in restored if r['label'] == 'handle /cloud/*')['upstream'] == 'http://mini-gateway-api:10000'
    assert next(r for r in restored if r['label'] == 'handle @admin_ui')['upstream'] == 'http://aimore-cloud:8766'
    assert history.query('api', now=NOW)['total_requests'] == 1
    assert len(list(store.directory('api').glob('connection-before-repair-*.json'))) == 1
    assert store.status('api', verify=True)['config_state'] == 'matched'


def test_repair_migrates_legacy_whole_site_binding_without_losing_new_routes(setup):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    record_path = store.directory('api') / 'connection.json'
    record = connections.read_record(record_path)
    record.update(rule_scoped=False, selector='legacy-selector', before=CADDY, after=path.read_text())
    atomic_write(record_path, json.dumps(record))
    path.write_text(CADDY + '\nother.test {\n    reverse_proxy other:8080\n}\n')
    result = store.repair({'key': 'api', 'token': plan['token']})
    assert result['status'] == 'repaired'
    assert 'reverse_proxy other:8080' in path.read_text()
    assert connections.read_record(record_path)['rule_scoped']


def test_repair_changed_backend_releases_stale_binding_without_overwriting(setup, monkeypatch):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    current = CADDY.replace('aimore-cloud:8766', 'new-backend:9090')
    path.write_text(current)
    monkeypatch.setattr(store, 'activate', lambda *_: pytest.fail('changed destination must not be replaced'))
    result = store.repair({'key': 'api', 'token': plan['token']})
    assert result['status'] == 'needs_selection'
    assert path.read_text() == current and store.status('api')['state'] == 'not_connected'


def test_repair_changed_matcher_scope_requires_selection(setup, monkeypatch):
    store, source, path = setup
    path.write_text(ZHUNKEDA.replace('{$PUBLIC_DOMAIN}', 'api.test'))
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    current = ZHUNKEDA.replace('{$PUBLIC_DOMAIN}', 'api.test').replace('@api path /ad/push/*', '@api path /*')
    path.write_text(current)
    monkeypatch.setattr(store, 'activate', lambda *_: pytest.fail('changed scope requires selection'))
    result = store.repair({'key': 'api', 'token': plan['token']})
    assert result['status'] == 'needs_selection' and path.read_text() == current


def test_gateway_moved_to_other_rule_is_not_treated_as_matching(setup):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    moved = path.read_text().replace('handle /cloud/*', 'handle /other/*')
    path.write_text(moved)
    assert store.status('api', verify=True)['config_state'] == 'changed'
    with pytest.raises(CertificateError, match='网关引用'):
        store.repair({'key': 'api', 'token': plan['token']})
    assert path.read_text() == moved and store.status('api')['state'] == 'connected'


def test_status_failure_is_cached_without_scanning_and_actions_retry_fresh(setup, monkeypatch):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    calls = []

    def unavailable(_):
        calls.append(True)
        raise CertificateError('配置没有 bind 挂载到宿主机')

    monkeypatch.setattr(connections, 'resolve', unavailable)
    monkeypatch.setattr(connections, 'discover', lambda: pytest.fail('polling must not scan all containers'))
    assert store.status('api', verify=True)['config_state'] == 'unavailable'
    assert store.status('api', verify=True)['config_state'] == 'unavailable'
    assert len(calls) == 1
    monkeypatch.setattr(connections, 'resolve', lambda _: dict(source))
    assert store.repair({'key': 'api', 'token': plan['token']})['status'] == 'repaired'
    assert store.status('api', verify=True)['config_state'] == 'matched'


def test_repair_restores_current_business_config_when_activation_fails(setup, monkeypatch):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    current = CADDY + '\n# latest business release\n'
    path.write_text(current)
    calls = []

    def activate(_):
        calls.append(True)
        if len(calls) == 1:
            raise CertificateError('restart failed')

    monkeypatch.setattr(store, 'activate', activate)
    with pytest.raises(CertificateError, match='原代理配置已恢复'):
        store.repair({'key': 'api', 'token': plan['token']})
    assert path.read_text() == current and len(calls) == 2
    assert list(store.directory('api').glob('before-repair-*.conf'))


def test_repair_unreadable_config_keeps_binding_and_explains_reason(setup, monkeypatch):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    original = path.read_bytes()

    def unavailable(_):
        raise CertificateError('配置没有 bind 挂载到宿主机')

    monkeypatch.setattr(connections, 'resolve', unavailable)
    monkeypatch.setattr(connections, 'discover', lambda: {'sources': []})
    status = store.status('api', verify=True)
    assert status['config_state'] == 'unavailable' and 'bind' in status['error']
    with pytest.raises(CertificateError, match='bind'):
        store.repair({'key': 'api', 'token': plan['token']})
    assert path.read_bytes() == original and store.status('api')['state'] == 'connected'


def test_repair_rejects_imports_without_retiring_binding(setup):
    store, source, path = setup
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    path.write_text(CADDY + '\nimport extra.caddy\n')
    with pytest.raises(CertificateError, match='导入'):
        store.repair({'key': 'api', 'token': plan['token']})
    assert store.status('api')['state'] == 'connected'


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
    assert store.status('api', verify=True)['config_state'] == 'matched'
    assert 'proxy_set_header Host $host;' in path.read_text()
    store.disconnect({'key': 'api', 'token': plan['token']})
    assert path.read_text() == original


def test_nginx_repair_preserves_new_locations_and_reuses_gateway(setup):
    store, source, path = setup
    source.update(kind='nginx', networks=['host'])
    path.write_text(NGINX)
    plan = preview(store, source, network='host')
    store.apply({'key': 'api', 'token': plan['token']})
    current = NGINX.replace('    location /api/ {', '    location /health { return 200; }\n    location /api/ {')
    path.write_text(current)
    assert store.repair({'key': 'api', 'token': plan['token']})['status'] == 'repaired'
    assert 'location /health { return 200; }' in path.read_text()
    assert 'proxy_pass http://127.0.0.1:18080;' in path.read_text()
    token = store.status('api')['token']
    store.disconnect({'key': 'api', 'token': token})
    assert path.read_text() == current


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


@pytest.mark.parametrize('action', ['apply', 'repair', 'disconnect', 'release'])
def test_connection_confirmation_is_required(setup, action):
    store, _, _ = setup
    with pytest.raises(CertificateError, match='确认'):
        store.operate({'action': action})


def test_status_follows_changed_caddy_startup_path(tmp_path, monkeypatch):
    config = tmp_path / 'relocated-Caddyfile'
    config.write_text(CADDY)
    item = {'Id': 'b' * 64, 'State': {'Status': 'running'},
            'Config': {'Cmd': ['caddy', 'run', '--config=/etc/caddy/current.conf']},
            'Mounts': [{'Type': 'bind', 'Source': str(config), 'Destination': '/etc/caddy/current.conf'}],
            'NetworkSettings': {'Networks': {'app_default': {}}}}
    monkeypatch.setattr(connections.nginx_runtime, 'require_local_docker', lambda: None)
    monkeypatch.setattr(connections, 'command', lambda args, **kw: json.dumps([item]) if 'inspect' in args else 'v2')
    raw = dict(kind='caddy', mode='docker', container='edge', config_path='/etc/caddy/Caddyfile', path='/old/file')
    source = connections.Connections(tmp_path / 'data').current_source({'source': raw})
    assert source['path'] == str(config) and source['config_path'] == '/etc/caddy/current.conf'


def test_source_rediscovery_rejects_ambiguous_container_identity(setup, monkeypatch):
    store, source, _ = setup
    record = {'source': source}

    def relocated(raw):
        if raw['container'] == 'edge':
            raise CertificateError('original container missing')
        return {**source, 'container': raw['container']}

    monkeypatch.setattr(connections, 'resolve', relocated)
    monkeypatch.setattr(connections, 'discover', lambda: {'sources': [{**source, 'container': name} for name in ('one', 'two')]})
    with pytest.raises(CertificateError, match='多个代理'):
        store.current_source(record)
    monkeypatch.setattr(connections, 'discover', lambda: {'sources': [{**source, 'container': 'one'}]})
    assert store.current_source(record)['container'] == 'one'


def test_repair_static_site_uses_existing_gateway_and_internal_port(setup):
    store, source, path = setup
    plan = preview(store, source, 'static')
    store.apply({'key': 'api', 'token': plan['token']})
    spec = store.gateways.read('api')['spec']
    path.write_text(CADDY + '\n# old reverse_proxy mini-gateway-api:10000\n')
    result = store.repair({'key': 'api', 'token': plan['token']})
    assert result['status'] == 'repaired'
    assert store.gateways.read('api')['spec']['upstream'] == spec['upstream']
    assert '# old reverse_proxy' in path.read_text()
    assert 'http://:18081' in path.read_text()


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


def test_fresh_docker_caddy_domain_is_automatically_resolved_without_saving_secrets(tmp_path, monkeypatch):
    config = tmp_path / 'Caddyfile'
    config.write_text(ZHUNKEDA, encoding='utf-8')
    item = {'Id': 'a' * 64, 'State': {'Status': 'running'},
            'Config': {'Entrypoint': [], 'Cmd': ['caddy', 'run', '--config', '/etc/caddy/Caddyfile'],
                       'Env': ['PUBLIC_DOMAIN=new.example.test', 'DATABASE_PASSWORD=private-secret']},
            'Mounts': [{'Type': 'bind', 'Source': str(config), 'Destination': '/etc/caddy/Caddyfile'}],
            'NetworkSettings': {'Networks': {'app_default': {}}}}
    monkeypatch.setattr(connections.nginx_runtime, 'require_local_docker', lambda: None)
    monkeypatch.setattr(connections, 'command', lambda args, **kw: json.dumps([item]) if 'inspect' in args else 'v2')
    raw = dict(kind='caddy', mode='docker', container='edge', config_path='/etc/caddy/Caddyfile')
    source = connections.resolve(raw)
    assert source['addresses'] == {'{$PUBLIC_DOMAIN}': 'new.example.test'}
    inspected = connections.Connections(tmp_path / 'data').inspect(raw)
    assert len(inspected['routes']) == 2 and all(route['supported'] for route in inspected['routes'])
    assert 'private-secret' not in json.dumps(inspected)
    config.write_text(ZHUNKEDA.replace('reverse_proxy api:8080', 'reverse_proxy mini-gateway-api:10000'))
    assert connections.resolve(raw) == source


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
