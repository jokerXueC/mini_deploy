"""Public onboarding, truthful verification, and narrowly authorized recovery."""
import json
from types import SimpleNamespace

import pytest

import gateway_connections as connections
import agent
import nginx_runtime
import request_gateway as gateway
import request_history as history
from certificates import CertificateError
from test_gateway_connections import CADDY, NGINX, preview, setup as setup
from test_request_history import NOW, ingest, line


@pytest.mark.parametrize('value', [['0.0.0.0/0'], ['example.test'], ['0.0.0.0'], ['224.0.0.1'],
                                 ['fe80::1%eth0'], ['fe80::1%eth0;\nserver {}'], [123], '127.0.0.1'])
def test_only_specific_proxy_addresses_can_be_trusted(value):
    with pytest.raises(CertificateError):
        gateway.normalize(dict(key='api', network='host', upstream='http://127.0.0.1:8000',
                               trust_proxy=True, trusted_proxies=value))


def test_real_ip_is_opt_in_and_old_managed_configuration_remains_upgradable(tmp_path, monkeypatch):
    base = dict(key='api', network='host', upstream='http://127.0.0.1:8000')
    assert 'set_real_ip_from' not in gateway.config(gateway.normalize(base))
    with pytest.raises(CertificateError):
        gateway.normalize({**base, 'trusted_proxies': ['127.0.0.1']})
    spec = gateway.normalize({**base, 'trust_proxy': True, 'trusted_proxies': ['127.0.0.1'] * 2})
    assert spec['trusted_proxies'] == ['127.0.0.1']
    text = gateway.config(spec)
    assert 'set_real_ip_from 127.0.0.1;' in text
    assert 'real_ip_recursive off;' in text and '"peer_ip":"$realip_remote_addr"' in text
    store = gateway.Store(tmp_path)
    store.save(spec)
    path = store.directory('api') / 'conf/nginx.conf'
    legacy = gateway.config(spec, _legacy=True)
    assert 'stderr crit;' in legacy and 'set_real_ip_from' not in legacy
    path.write_text(legacy, encoding='utf-8')
    calls = []
    monkeypatch.setattr(store, 'reload', calls.append)
    store.upgrade_logging(store.read('api'), {'Id': 'a' * 64, 'State': {'Running': True}})
    assert path.read_text(encoding='utf-8') == text and calls == ['a' * 64]


def test_nginx_multiple_domains_and_header_inheritance_follow_nginx_array_rules():
    inherited = '''server {
      server_name api.test www.api.test;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_connect_timeout 12;
      location /api/ { proxy_pass http://api:8000; }
    }'''
    route = connections.routes(inherited, 'nginx')[0]
    assert route['supported'] and route['domains'] == ['api.test', 'www.api.test']
    assert route['_connect_timeout'] == 12000
    partial = inherited.replace('location /api/ {', 'location /api/ { proxy_set_header Host $host;')
    assert not connections.routes(partial, 'nginx')[0]['supported']


def test_panel_generated_dynamic_dns_stays_connectable_without_accepting_arbitrary_variables():
    text = agent._nginx_http_config_text('api.test', 'api', 8000, docker_dns=True)
    route = connections.routes(text, 'nginx')[0]
    assert route['supported'] and route['upstream'] == 'http://api:8000'
    text = text.replace('set $mini_deploy_backend http://api:8000;', 'set $mini_deploy_backend $http_host;')
    assert not connections.routes(text, 'nginx')[0]['supported']
    text = agent._nginx_http_config_text('api.test', 'api', 8000, docker_dns=True)
    text = text.replace('proxy_pass', 'if ($arg_test) { set $mini_deploy_backend http://other:8000; } proxy_pass')
    assert not connections.routes(text, 'nginx')[0]['supported']


@pytest.mark.parametrize('kind,text', [('caddy', CADDY), ('nginx', NGINX)])
def test_automatic_hop_keeps_uploads_unlimited_and_trusts_only_frontend(setup, kind, text):
    store, source, path = setup
    source.update(kind=kind, proxy_addresses={'app_default': '172.20.0.5'})
    path.write_text(text.replace('127.0.0.1:8000', 'api:8000'), encoding='utf-8')
    plan = preview(store, source)
    spec = plan['gateway']
    assert spec['max_body_bytes'] == 0 and spec['trusted_proxies'] == ['172.20.0.5']
    assert spec['connect_timeout_ms'] == (3000 if kind == 'caddy' else 60000)


@pytest.mark.parametrize('status,expected,accepted,verified', [
    (503, None, False, False), (404, None, True, False),
    (401, None, True, False), (200, 204, False, False), (204, 204, True, True),
])
def test_probe_rejects_server_errors_and_does_not_call_reachability_health(monkeypatch, status, expected, accepted, verified):
    requests = []
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            requests.append(kwargs['headers'])

        def getresponse(self):
            return SimpleNamespace(status=status)

        def close(self):
            pass

    monkeypatch.setattr(connections.http.client, 'HTTPConnection', Client)
    monkeypatch.setattr(connections.time, 'sleep', lambda _: None)
    plan = dict(spec={'port': 18080}, probe_path='/health', site='api.test', expected_status=expected, probe_id='test-probe')
    if accepted:
        result = connections.Connections.probe(None, plan)
        assert result['status'] == status and result['application_verified'] is verified
    else:
        with pytest.raises(CertificateError, match='HTTP ' + str(status)):
            connections.Connections.probe(None, plan)
    assert all(item['X-Request-ID'] == 'test-probe' for item in requests)


@pytest.mark.parametrize('approved,changed_backend', [(False, False), (True, True), (True, False)])
def test_restore_requires_prior_approval_and_exact_original_backend(setup, approved, changed_backend):
    store, source, path = setup
    plan = preview(store, source, auto_restore=approved)
    store.apply({'key': 'api', 'token': plan['token']})
    published = CADDY.replace('aimore-cloud:8766', 'new-backend:8888') if changed_backend else CADDY
    path.write_text(published, encoding='utf-8')
    status = store.maintain('api', store.status('api', verify=True))
    if approved and not changed_backend:
        assert status['config_state'] == 'matched'
        assert 'mini-gateway-api:10000' in path.read_text(encoding='utf-8')
    else:
        assert path.read_text(encoding='utf-8') == published
        assert status['config_state'] == 'changed'


def test_clean_failed_repair_preserves_retry_authorization(setup, monkeypatch):
    store, source, path = setup
    plan = preview(store, source, auto_restore=True)
    store.apply({'key': 'api', 'token': plan['token']})
    path.write_text(CADDY, encoding='utf-8')
    def fail(*args):
        raise CertificateError('temporary network error')
    monkeypatch.setattr(store, 'probe', fail)
    with pytest.raises(CertificateError):
        store.maintain('api', store.status('api', verify=True))
    record = connections.read_record(store.directory('api') / 'connection.json')
    assert record['state'] == 'connected' and record['auto_restore'] is True
    assert record['token'] == plan['token'] and path.read_text(encoding='utf-8') == CADDY
    monkeypatch.setattr(store, 'probe', lambda *_: None)
    assert store.maintain('api', store.status('api', verify=True))['config_state'] == 'matched'


def test_failed_rollback_is_not_hidden_by_restoring_old_connected_metadata(setup, monkeypatch):
    store, source, path = setup
    plan = preview(store, source, auto_restore=True)
    store.apply({'key': 'api', 'token': plan['token']})
    path.write_text(CADDY, encoding='utf-8')
    calls = []
    def validate(*args):
        calls.append(args)
        if len(calls) > 1:
            raise CertificateError('cannot validate applied or rollback configuration')
    monkeypatch.setattr(store, 'validate', validate)
    with pytest.raises(CertificateError, match='恢复未完成'):
        store.maintain('api', store.status('api', verify=True))
    record = connections.read_record(store.directory('api') / 'connection.json')
    assert record['state'] == 'recovery_required'


def test_background_restore_cannot_gain_manual_repair_permissions(setup, monkeypatch):
    store, source, path = setup
    plan = preview(store, source, auto_restore=True)
    store.apply({'key': 'api', 'token': plan['token']})
    seen = []
    original = store.current_config
    def current(record, **options):
        seen.append(options.get('repair_permissions', False))
        return original(record, **options)
    monkeypatch.setattr(store, 'current_config', current)
    path.write_text(CADDY, encoding='utf-8')
    store.repair({'key': 'api', 'token': plan['token']}, _automatic=True)
    assert seen and not any(seen)


def test_proxy_recreation_updates_only_gateway_trusted_address(setup):
    store, source, path = setup
    source['proxy_addresses'] = {'app_default': '172.20.0.5'}
    plan = preview(store, source)
    store.apply({'key': 'api', 'token': plan['token']})
    connected = path.read_text(encoding='utf-8')
    source['proxy_addresses'] = {'app_default': '172.20.0.9'}
    store.maintain('api', store.status('api', verify=True))
    assert store.gateways.read('api')['spec']['trusted_proxies'] == ['172.20.0.9']
    assert path.read_text(encoding='utf-8') == connected
    spec = store.gateways.read('api')['spec']
    with pytest.raises(CertificateError, match='后台核对'):
        store.gateways.save({**spec, 'upstream': 'http://other:8000'}, gateway.revision(spec), _reconcile=True)
    assert store.gateways.read('api')['spec'] == spec


def test_actual_nginx_virtual_host_files_and_custom_startup_are_discovered(monkeypatch):
    dump = '''# configuration file /etc/nginx/nginx.conf:
    http { include sites-enabled/*; }
    # configuration file /etc/nginx/sites-enabled/api:
    server { server_name api.test; }
    # configuration file /etc/nginx/mime.types:
    types { text/html html; }
    # configuration file /srv/nginx/web.conf:
    server { server_name web.test; }
    '''
    dump = '\n'.join(part.strip() for part in dump.splitlines())
    assert nginx_runtime.configuration_files(dump) == ['/etc/nginx/sites-enabled/api', '/srv/nginx/web.conf']
    assert nginx_runtime.startup_options(['nginx', '-c/srv/nginx.conf', '-p', '/srv/']) == ['-c', '/srv/nginx.conf', '-p', '/srv/']
    monkeypatch.setattr(connections.shutil, 'which', lambda _: None)
    monkeypatch.setattr(nginx_runtime, 'require_local_docker', lambda: None)
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        if 'ps' in args:
            return json.dumps({'Names': 'edge', 'Image': 'custom-image'})
        if 'top' in args:
            return 'COMMAND\nnginx: master process nginx -c /srv/nginx.conf'
        if 'inspect' in args:
            return json.dumps({'Entrypoint': ['nginx'], 'Cmd': ['-c', '/srv/nginx.conf']})
        if '-T' in args:
            return dump
        raise AssertionError(args)
    monkeypatch.setattr(connections, 'command', command)
    result = connections.discover()
    assert [s['config_path'] for s in result['sources']] == nginx_runtime.configuration_files(dump)
    assert ['docker', 'exec', 'edge', 'nginx', '-T', '-c', '/srv/nginx.conf'] in calls
    assert all('.Env' not in ' '.join(call) for call in calls)
    assert result['checks'][0]['state'] == 'ready'


def test_caddy_equals_config_and_resume_are_not_silently_defaulted(monkeypatch):
    monkeypatch.setattr(connections.shutil, 'which', lambda name: '/usr/bin/caddy' if name == 'caddy' else None)
    monkeypatch.setattr(nginx_runtime, 'require_local_docker', lambda: (_ for _ in ()).throw(CertificateError('no Docker')))
    startup = ['caddy run --config=/srv/Caddyfile']
    monkeypatch.setattr(connections, 'command', lambda *args, **kwargs: startup[0])
    result = connections.discover()
    assert result['sources'][0]['config_path'] == '/srv/Caddyfile'
    startup[0] += ' --resume'
    result = connections.discover()
    assert result['sources'] == [] and any('resume' in error for error in result['errors'])


def test_discovery_time_budget_returns_partial_results_instead_of_client_timeout(monkeypatch):
    clock = [0]
    monkeypatch.setattr(connections.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(connections.shutil, 'which', lambda _: None)
    monkeypatch.setattr(nginx_runtime, 'require_local_docker', lambda: None)
    def command(args, **options):
        assert options['timeout'] <= 4
        if 'ps' in args:
            return '\n'.join(json.dumps({'Names': name, 'Image': 'caddy'}) for name in ('edge-one', 'edge-two'))
        clock[0] = 14
        return json.dumps({'Cmd': ['caddy', 'run', '--config', '/srv/Caddyfile']})
    monkeypatch.setattr(connections, 'command', command)
    result = connections.discover()
    assert len(result['sources']) == 1 and result['sources'][0]['container'] == 'edge-one'
    assert any('时间预算' in error and '1' in error for error in result['errors'])


def test_collection_status_distinguishes_probe_real_traffic_and_stale_sampler(tmp_path):
    store = history.History(tmp_path)
    connection = {'applied_at': NOW - 20, 'probe_id': 'synthetic-probe'}
    assert store.collection_status('api', connection, NOW)['state'] == 'pending'
    payload = line().replace('"method": "GET"', '"request_id": "synthetic-probe", "method": "GET"')
    ingest(store, [payload])
    assert store.collection_status('api', connection, NOW)['state'] == 'waiting'
    ingest(store, [line('/real')])
    assert store.collection_status('api', connection, NOW)['state'] == 'active'
    assert store.collection_status('api', connection, NOW + 120)['state'] == 'stale'
    store.ingest('api', 'API', 'container-a', [], NOW + 69, now=NOW + 70)
    assert store.collection_status('api', connection, NOW + 70)['state'] == 'idle'
    store.failure('api', 'API', 'log rotation failed')
    assert store.collection_status('api', connection, NOW + 70)['state'] == 'error'
