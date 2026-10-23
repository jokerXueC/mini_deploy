"""Opt-in rules, rollback and drift handling without changing the host firewall."""
import json
import http.client
import threading
from types import SimpleNamespace

import pytest

import agent
import basic_protection as basic
from login_guard import DEFAULTS
from test_login_guard import guard as guard
from test_deployment_removal import http_server as http_server, runtime as runtime


class Firewall:
    def __init__(self):
        self.calls = []
        self.fail = False

    def apply(self, *args):
        if self.fail:
            raise RuntimeError('模拟防火墙失败')
        self.calls.append(args)


@pytest.fixture
def setup(guard, tmp_path, monkeypatch):
    engine, now, _ = guard
    conf = tmp_path / 'nginx.conf'
    conf.write_text('events {}\nhttp {\n server { listen 80; location / { proxy_pass http://127.0.0.1:8766; } }\n}\n')
    # Windows absolute fixture paths are mapped explicitly; production requires POSIX paths.
    monkeypatch.setattr(basic, 'mapped_path', lambda rt, visible: conf)
    dump = '# configuration file /etc/nginx/nginx.conf:\n' + conf.read_text()
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return dump if '-T' in args else ''

    monkeypatch.setattr(basic.proxy, 'command', command)
    fw = Firewall()
    runtime = {'kind': 'nginx', 'mode': 'local', 'label': '本机 Nginx', 'prefix': []}
    protection = basic.Protection(tmp_path, engine, firewall=fw,
        discovery=lambda: ([runtime, {'kind': 'caddy', 'label': '业务入口'}], [80, 443], []), clock=lambda: now[0])
    return protection, conf, calls, fw


def finish(protection, enabled=True, ip='1.1.1.1'):
    protection.start(enabled, ip, {'ready': True, 'ports': [2222]})
    protection.worker.join(timeout=5)
    assert not protection.worker.is_alive()
    return protection.status()


def test_default_off_enable_preserves_business_and_disable_restores(setup):
    protection, conf, calls, fw = setup
    original = conf.read_text()
    assert not protection.status()['enabled'] and not calls and not fw.calls
    state = finish(protection)
    assert state['enabled'] and not state['busy']
    assert state['connections']['state'] == 'active'
    assert state['entries'][0]['state'] == 'active'
    assert state['entries'][1]['state'] == 'unavailable'
    assert state['downloads']['state'] == 'off'
    assert 'proxy_pass http://127.0.0.1:8766' in conf.read_text()
    assert 'limit_req_status 429' in conf.read_text()
    assert '1.1.1.1/32 1;' in conf.read_text()
    assert not {'records', 'ignores', 'previous_login'} & state.keys()
    assert finish(protection, False)['phase'] == 'off'
    assert conf.read_text() == original and conf.exists()
    assert fw.calls[-1] == ([], [], False)
    assert protection.guard.status()['settings']['enabled'] == DEFAULTS['enabled']


def test_reload_failure_rolls_back_and_keeps_recovery_record(setup, monkeypatch):
    protection, conf, calls, _ = setup
    original = conf.read_text()
    command = basic.proxy.command
    failed = []

    def fail_once(args, **kwargs):
        if args[-1] == 'reload' and not failed:
            failed.append(True)
            raise ValueError('模拟重载失败')
        return command(args, **kwargs)

    monkeypatch.setattr(basic.proxy, 'command', fail_once)
    state = finish(protection)
    assert state['entries'][0]['state'] == 'unavailable'
    assert conf.read_text() == original
    assert protection.state['records'][0]['files'][0]['before'].replace('\r\n', '\n') == original
    assert finish(protection, False)['phase'] == 'off'


def test_external_change_never_overwritten_and_reports_partial_close(setup):
    protection, conf, _, _ = setup
    finish(protection)
    external = conf.read_text() + '\n# business redeploy\n'
    conf.write_text(external)
    protection.check()
    assert protection.status()['entries'][0]['state'] == 'changed'
    assert finish(protection, False)['phase'] == 'partial'
    assert conf.read_text() == external


def test_reconcile_updates_ports_and_whitelist_and_preserves_user_policy_on_disable(setup):
    protection, conf, _, fw = setup
    finish(protection)
    settings = protection.guard.status()['settings']
    protection.guard.change({**settings, 'action': 'settings', 'maxretry': 11,
                             'ignoreip': [*settings['ignoreip'], '8.8.8.8']}, '1.1.1.1')
    discovery = protection.discover
    protection.discover = lambda: (discovery()[0], [443], [])
    protection.check()
    assert fw.calls[-1][0] == [443] and '8.8.8.8/32' in fw.calls[-1][1]
    assert '8.8.8.8/32 1;' in conf.read_text()
    assert finish(protection, False)['phase'] == 'off'
    settings = protection.guard.status()['settings']
    assert settings['maxretry'] == 11 and '8.8.8.8/32' in settings['ignoreip']


def test_newly_discovered_unsupported_entry_is_visible(setup):
    protection, _, _, _ = setup
    finish(protection)
    discovery = protection.discover
    protection.discover = lambda: ([*discovery()[0], {'kind': 'caddy', 'label': 'new-entry'}], [80, 443], [])
    protection.check()
    assert any(item['label'] == 'new-entry' for item in protection.status()['entries'])


def test_unchanged_periodic_check_does_not_reload_nginx(setup):
    protection, _, calls, _ = setup
    finish(protection)
    before = sum(args[-1] == 'reload' for args in calls)
    protection.check()
    assert sum(args[-1] == 'reload' for args in calls) == before


def test_restart_reconcile_firewall_failure_and_private_configuration(setup):
    protection, conf, _, fw = setup
    finish(protection)
    restored = basic.Protection(protection.home, protection.guard, firewall=fw, discovery=protection.discover)
    assert restored.status()['enabled']
    fw.fail = True
    restored.check()
    assert restored.status()['connections']['state'] == 'unavailable'
    assert 'proxy_pass' not in json.dumps(restored.status())
    fw.fail = False
    restored.request_check()
    restored.worker.join(timeout=5)
    assert not restored.worker.is_alive()
    assert restored.status()['connections']['state'] == 'active'


def test_existing_limits_and_proxy_real_ip_are_not_overwritten(setup):
    protection, conf, _, _ = setup
    for directive in ['limit_req zone=existing;', 'real_ip_header X-Forwarded-For;', 'limit_rate 10m;']:
        original = 'events {}\nhttp { server { listen 80; ' + directive + ' } }\n'
        conf.write_text(original)
        state = finish(protection)
        assert state['entries'][0]['state'] == 'unavailable'
        assert conf.read_text() == original


def test_connection_batch_is_atomic_scoped_ipv6_and_docker_forward():
    batches, foreign = [], [False]

    def run(args, **kwargs):
        if args[-1] == 'tables':
            value = {'nftables': [{'table': {'family': 'inet', 'name': basic.TABLE}}]}
        elif args[-1] == basic.TABLE:
            value = {'nftables': [{'table': {'comment': 'foreign' if foreign[0] else basic.OWNER}}]}
        else:
            batches.append((args, kwargs['input']))
            value = {}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    fw = basic.ConnectionsFirewall(runner=run, which=lambda _: 'nft', supported=True)
    fw.apply([80, 443], ['1.1.1.1/32', '2001:4860::1/128'], True)
    assert len(batches) == 2 and '-c' in batches[0][0]
    content = batches[1][1]
    assert 'flush ruleset' not in content
    assert 'delete table inet ' + basic.TABLE in content
    assert 'hook forward' in content and 'ct direction original ct status dnat ct original proto-dst { 80, 443 }' in content
    assert 'ipv6_addr' in content and '1.1.1.1/32 } return' in content
    assert 'ct state new' in content and 'limit rate over 60/second burst 120 packets' in content
    foreign[0] = True
    with pytest.raises(RuntimeError, match='归属'):
        fw.apply([], [], False)
    assert len(batches) == 2


def test_paths_reject_unmounted_config_and_symlinks(tmp_path):
    with pytest.raises(ValueError, match='未挂载'):
        basic.mapped_path({'mode': 'docker', 'mounts': []}, '/etc/nginx/nginx.conf')
    with pytest.raises(ValueError):
        basic.mapped_path({'mode': 'local'}, '/etc/../nginx.conf')
    path = tmp_path / 'main.conf'
    path.write_text('http {}')
    rt = {'mode': 'docker', 'mounts': [{'Type': 'volume', 'Destination': '/etc/nginx', 'Source': str(tmp_path)}]}
    assert basic.mapped_path(rt, '/etc/nginx/main.conf') == path


def test_nginx_included_sites_receive_limits_without_changing_proxy_rules(tmp_path):
    main = tmp_path / 'nginx.conf'
    site = tmp_path / 'site.conf'
    main.write_text('events {}\nhttp { include /etc/nginx/site.conf; }\n')
    site.write_text('server { listen 80; location / { proxy_pass http://api:8766; } }\n')
    original = site.read_text()
    runtime = {'mode': 'docker', 'mounts': [{'Type': 'bind', 'Source': str(tmp_path), 'Destination': '/etc/nginx'}]}
    dump = '# configuration file /etc/nginx/nginx.conf:\n' + main.read_text() + '# configuration file /etc/nginx/site.conf:\n' + original
    files, servers = basic.nginx_candidates(runtime, dump, ['127.0.0.0/8', '::1/128'])
    assert servers == 1 and len(files) == 2
    assert 'limit_req_zone' in files[0]['after'] and 'limit_req zone=' not in files[0]['after']
    assert 'limit_req zone=' in files[1]['after'] and 'proxy_pass http://api:8766;' in files[1]['after']
    assert site.read_text() == original


def test_authentication_csrf_and_no_host_changes_on_get(http_server, setup, monkeypatch):
    protection, _, calls, fw = setup
    monkeypatch.setattr(agent, '_basic_protection', lambda: protection)
    assert http_server('GET', '/security/protection', authenticated=False)[0] == 401
    assert http_server('POST', '/security/protection', {'enabled': True}, authenticated=False)[0] == 401
    code, state = http_server('GET', '/security/protection')
    assert code == 200 and not state['enabled'] and not calls and not fw.calls
    assert http_server('POST', '/security/protection', {'enabled': 'yes'})[0] == 400
    connection = http.client.HTTPConnection(http_server.origin.removeprefix('http://'), timeout=5)
    connection.request('POST', '/security/protection', json.dumps({'enabled': True}),
        {'Content-Type': 'application/json', 'Cookie': agent.COOKIE_NAME + '=' + http_server.cookie})
    response = connection.getresponse()
    assert response.status == 403
    response.read()
    connection.close()
    assert not protection.status()['enabled'] and not calls


def test_slow_detection_remains_pollable_and_duplicate_start_is_rejected(setup):
    protection, _, _, _ = setup
    started, release = threading.Event(), threading.Event()

    def slow_discovery():
        started.set()
        release.wait(timeout=5)
        return [], [], []

    protection.discover = slow_discovery
    try:
        protection.start(True, '1.1.1.1', {})
        assert started.wait(timeout=2)
        state = protection.status()
        assert state['busy'] and state['phase'] == 'applying'
        with pytest.raises(ValueError, match='正在处理'):
            protection.start(True, '1.1.1.1', {})
    finally:
        release.set()
        protection.worker.join(timeout=5)
    assert not protection.worker.is_alive()
