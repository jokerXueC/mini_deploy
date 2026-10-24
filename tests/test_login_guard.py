"""Policy persistence and actual enforcement decisions without touching host rules."""
import json
import http.client
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

import agent
from login_guard import DEFAULTS, Firewall, Guard, OWNER, TABLE
from security_observation import History
from test_deployment_removal import http_server as http_server, runtime as runtime


class Kernel:
    def __init__(self):
        self.calls = []
        self.fail = False

    def apply(self, ports, bans, now):
        if self.fail:
            raise RuntimeError('模拟内核拒绝')
        self.calls.append((ports, bans, now))


@pytest.fixture
def guard(tmp_path):
    now, kernel = [1800000000.], Kernel()
    guard = Guard(History(tmp_path), clock=lambda: now[0], firewall=kernel,
                  locator=lambda ip: {'label': '测试地区', 'latitude': 30, 'longitude': 120})
    return guard, now, kernel


def fail(guard, now, ip='8.8.8.8', count=5, source='panel'):
    for _ in range(count):
        now[0] += .1
        if source == 'panel':
            guard.record_panel(ip, False)
        else:
            with guard.history.connection() as db:
                guard._event(db, source, 'login_failure', ip, 'SSH 认证失败', now[0])


def test_panel_failure_threshold_real_block_restart_and_expiry(guard):
    engine, now, kernel = guard
    fail(engine, now, count=4)
    assert not engine.blocked('8.8.8.8')
    fail(engine, now, count=1)
    assert engine.blocked('8.8.8.8')
    assert not engine.blocked('1.1.1.1')
    restored = Guard(engine.history, clock=lambda: now[0], firewall=kernel)
    assert restored.blocked('8.8.8.8')
    assert restored.status()['failure_24h'] == 5
    now[0] += 3601
    assert not restored.blocked('8.8.8.8')
    restored.tick({'ready': True, 'ports': [2222]})
    assert restored.status()['ban_total'] == 0


def test_manual_ban_applies_both_scopes_persists_does_not_extend_and_expires(guard):
    engine, now, kernel = guard
    engine.tick({'ready': True, 'ports': [2222]})
    engine.change({'action': 'ban', 'ip': '8.8.8.8', 'source': 'all'}, '1.1.1.1')
    assert engine.blocked('8.8.8.8')
    state = engine.status()
    assert state['failure_24h'] == 0 and state['ban_total'] == 1
    assert {row['source'] for row in state['bans']} == {'panel', 'ssh'}
    assert all(row['enforced'] and row['reason'] == '管理员手动封禁' for row in state['bans'])
    assert kernel.calls[-1][0] == [2222] and kernel.calls[-1][1][0]['ip'] == '8.8.8.8'
    expires = {row['source']: row['expires'] for row in state['bans']}
    now[0] += 30
    engine.change({'action': 'ban', 'ip': '8.8.8.8', 'source': 'all'}, '1.1.1.1')
    assert {row['source']: row['expires'] for row in engine.status()['bans']} == expires
    restored = Guard(engine.history, clock=lambda: now[0], firewall=kernel)
    assert restored.blocked('8.8.8.8')
    now[0] += DEFAULTS['bantime']
    restored.tick({'ready': True, 'ports': [2222]})
    assert not restored.blocked('8.8.8.8') and restored.status()['ban_total'] == 0


def test_manual_ban_guards_current_ip_whitelist_and_unavailable_ssh(guard):
    engine, _, _ = guard
    engine.change({**DEFAULTS, 'action': 'settings', 'ignoreip': ['9.9.9.0/24']}, '1.1.1.1')
    for ip in ('1.1.1.1', '::ffff:1.1.1.1', '127.0.0.1', '0.0.0.0', '224.0.0.1', '9.9.9.9'):
        with pytest.raises(ValueError, match='不能封禁'):
            engine.change({'action': 'ban', 'ip': ip, 'source': 'panel'}, '1.1.1.1')
    for source in ('all', 'ssh'):
        with pytest.raises(ValueError, match='SSH 防护尚未就绪'):
            engine.change({'action': 'ban', 'ip': '8.8.8.8', 'source': source}, '1.1.1.1')
    assert engine.status()['ban_total'] == 0
    for source in ([], 'invalid'):
        with pytest.raises(ValueError, match='来源无效'):
            engine.change({'action': 'ban', 'ip': '8.8.8.8', 'source': source}, '1.1.1.1')
    engine.change({'action': 'ban', 'ip': '8.8.8.8', 'source': 'panel'}, '1.1.1.1')
    assert engine.blocked('8.8.8.8') and engine.status()['ban_total'] == 1
    engine.change({**DEFAULTS, 'action': 'settings', 'enabled': False}, '1.1.1.1')
    with pytest.raises(ValueError, match='暂停'):
        engine.change({'action': 'ban', 'ip': '8.8.4.4', 'source': 'panel'}, '1.1.1.1')


def test_manual_ban_kernel_failure_rolls_back_both_scopes_and_history(guard):
    engine, _, kernel = guard
    engine.tick({'ready': True, 'ports': [2222]})
    kernel.fail = True
    with pytest.raises(RuntimeError):
        engine.change({'action': 'ban', 'ip': '8.8.8.8', 'source': 'all'}, '1.1.1.1')
    assert not engine.blocked('8.8.8.8') and engine.status()['ban_total'] == 0
    assert engine.status()['bans_24h'] == 0


def test_manual_ban_status_is_available_for_map_ips_beyond_current_ban_page(guard):
    engine, _, _ = guard
    engine.record_panel('8.8.8.8', True)
    engine.change({'action': 'ban', 'ip': '8.8.8.8', 'source': 'panel'}, '1.1.1.1')
    state = engine.status(ban_page=1)
    assert state['bans'] == []
    assert state['locations'][0]['bans'] == [{'source': 'panel', 'enforced': True}]
    assert not state['locations'][0]['protected']


def test_ban_pagination_keeps_both_scopes_together_and_counts_ips(guard):
    engine, now, _ = guard
    engine.tick({'ready': True, 'ports': [2222]})
    for index in range(11):
        now[0] += 1
        engine.change({'action': 'ban', 'ip': f'8.8.8.{index + 1}', 'source': 'all'}, '1.1.1.1')
    first, second = engine.status(), engine.status(ban_page=1)
    assert first['ban_total'] == second['ban_total'] == 11
    assert len(first['bans']) == 20 and len(second['bans']) == 2
    assert not {row['ip'] for row in first['bans']} & {row['ip'] for row in second['bans']}
    assert {row['source'] for row in second['bans']} == {'panel', 'ssh'}


def test_combined_unban_is_atomic_and_keeps_other_ips(guard):
    engine, now, kernel = guard
    engine.tick({'ready': True, 'ports': [2222]})
    engine.change({'action': 'ban', 'ip': '8.8.8.8', 'source': 'panel'}, '1.1.1.1')
    now[0] += 60
    engine.change({'action': 'ban', 'ip': '8.8.8.8', 'source': 'ssh'}, '1.1.1.1')
    engine.change({'action': 'ban', 'ip': '9.9.9.9', 'source': 'all'}, '1.1.1.1')
    before = engine.status()['bans']
    assert len({row['expires'] for row in before if row['ip'] == '8.8.8.8'}) == 2
    kernel.fail = True
    with pytest.raises(RuntimeError):
        engine.change({'action': 'unban', 'ip': '8.8.8.8', 'source': 'all'}, '1.1.1.1')
    assert engine.status()['bans'] == before and engine.blocked('8.8.8.8')
    with engine.history.connection(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM security_activity WHERE kind='unban'").fetchone()[0] == 0
    kernel.fail = False
    engine.change({'action': 'unban', 'ip': '8.8.8.8', 'source': 'all'}, '1.1.1.1')
    assert not engine.blocked('8.8.8.8') and engine.blocked('9.9.9.9')
    assert {row['ip'] for row in engine.status()['bans']} == {'9.9.9.9'}
    assert {row['ip'] for row in kernel.calls[-1][1]} == {'9.9.9.9'}


def test_successes_loopback_whitelist_and_sources_do_not_create_false_bans(guard):
    engine, now, _ = guard
    for _ in range(12):
        engine.record_panel('8.8.8.8', True)
    assert not engine.blocked('8.8.8.8')
    fail(engine, now, '127.0.0.1')
    assert not engine.blocked('127.0.0.1')
    engine.change({**DEFAULTS, 'action': 'settings', 'ignoreip': ['8.8.8.0/24']}, '1.1.1.1')
    fail(engine, now)
    assert not engine.blocked('8.8.8.8')
    assert '1.1.1.1/32' in engine.status()['settings']['ignoreip']
    fail(engine, now, '9.9.9.9', count=3)
    fail(engine, now, '9.9.9.9', count=3, source='ssh')
    engine.tick({'ready': True, 'ports': [2222]})
    assert not engine.blocked('9.9.9.9')
    assert engine.status()['ban_total'] == 0


def test_ssh_kernel_sync_ipv6_no_replay_reban_and_failed_unban_is_not_hidden(guard):
    engine, now, kernel = guard
    fail(engine, now, '2001:4860:4860::8888', source='ssh')
    engine.tick({'ready': True, 'ports': [2222]})
    row = engine.status()['bans'][0]
    assert row['source'] == 'ssh' and row['enforced']
    assert kernel.calls[-1][0] == [2222]
    assert kernel.calls[-1][1][0]['ip'] == row['ip']
    assert not engine.blocked(row['ip'])
    kernel.fail = True
    with pytest.raises(RuntimeError):
        engine.change({'action': 'unban', 'ip': row['ip'], 'source': 'ssh'}, '1.1.1.1')
    assert engine.status()['ban_total'] == 1
    kernel.fail = False
    now[0] += .1
    engine.change({'action': 'unban', 'ip': row['ip'], 'source': 'ssh'}, '1.1.1.1')
    engine.tick({'ready': True, 'ports': [2222]})
    assert engine.status()['ban_total'] == 0
    assert kernel.calls[-1][1] == []
    fail(engine, now, row['ip'], count=4, source='ssh')
    engine.tick({'ready': True, 'ports': [2222]})
    assert engine.status()['ban_total'] == 0
    fail(engine, now, row['ip'], count=1, source='ssh')
    engine.tick({'ready': True, 'ports': [2222]})
    assert engine.status()['ban_total'] == 1


def test_stale_backfill_does_not_ban_and_kernel_failure_is_explicit(guard):
    engine, now, kernel = guard
    fail(engine, now, source='ssh')
    now[0] += 100
    engine.tick({'ready': True, 'ports': [22]})
    assert engine.status()['ban_total'] == 0
    fail(engine, now, source='ssh')
    kernel.fail = True
    engine.tick({'ready': True, 'ports': [22]})
    state = engine.status()
    assert not state['ssh']['ready'] and not state['bans'][0]['enforced']
    assert '模拟内核拒绝' in state['ssh']['detail']


def test_new_ssh_ban_not_claimed_enforced_before_sync_and_restart_pause_flushes(guard):
    engine, now, kernel = guard
    engine.tick({'ready': True, 'ports': [2222]})
    fail(engine, now, source='ssh')
    engine.record_panel('1.1.1.1', True)
    assert engine.status()['bans'][0]['enforced'] is False
    engine.tick({'ready': True, 'ports': [2222]})
    assert engine.status()['bans'][0]['enforced'] is True
    restored = Guard(engine.history, clock=lambda: now[0], firewall=kernel)
    kernel.fail = True
    with pytest.raises(RuntimeError):
        restored.change({**DEFAULTS, 'action': 'settings', 'enabled': False}, '1.1.1.1')
    assert restored.status()['settings']['enabled']
    assert restored.status()['ban_total'] == 1
    kernel.fail = False
    restored.change({**DEFAULTS, 'action': 'settings', 'enabled': False}, '1.1.1.1')
    assert restored.status()['ban_total'] == 0 and kernel.calls[-1][0] == []


def test_geography_bounded_cached_and_private_addresses_not_sent(guard):
    engine, now, _ = guard
    calls = []
    def locate(ip):
        calls.append(ip)
        return {'label': '测试地区', 'latitude': 30, 'longitude': 120}
    engine.locator = locate
    for ip in ['8.8.8.8', '1.1.1.1', '9.9.9.9', '192.168.1.1']:
        engine.record_panel(ip, True)
    engine.update_locations()
    assert len(calls) == 2 and '192.168.1.1' not in calls
    engine.update_locations()
    assert len(calls) == 3
    engine.update_locations()
    assert len(calls) == 3
    assert engine.status()['location_total'] == 4
    assert len([row for row in engine.status()['locations'] if 'latitude' in row]) == 3


def test_pause_removes_only_owned_bans_and_keeps_login_records(guard):
    engine, now, kernel = guard
    fail(engine, now)
    fail(engine, now, source='ssh')
    engine.tick({'ready': True, 'ports': [22]})
    engine.change({**DEFAULTS, 'action': 'settings', 'enabled': False}, '1.1.1.1')
    assert not engine.blocked('8.8.8.8')
    assert engine.status()['ban_total'] == 0
    assert engine.status()['failure_24h'] == 10
    assert kernel.calls[-1][0] == []


def test_nft_batch_is_scoped_atomic_ipv4_ipv6_and_refuses_foreign_table():
    batches, foreign = [], [False]
    def runner(args, **kw):
        if args[-1] == 'tables':
            data = {'nftables': [{'table': {'family': 'inet', 'name': TABLE}}]}
        elif args[-1] == TABLE:
            data = {'nftables': [{'table': {'comment': 'foreign' if foreign[0] else OWNER}}]}
        else:
            batches.append(kw['input'])
            data = {}
        return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr='')
    firewall = Firewall(runner=runner, which=lambda _: 'nft', supported=True)
    firewall.apply([22, 2222], [{'ip': '8.8.8.8', 'expires': 109.2}, {'ip': '2001:4860::8888', 'expires': 120}], 100)
    batch = batches[0]
    assert 'flush ruleset' not in batch and 'delete table inet ' + TABLE in batch
    assert 'ct state new tcp dport { 22, 2222 } ip6 saddr @blocked6 drop' in batch
    assert '8.8.8.8 timeout 10s' in batch and '2001:4860::8888 timeout 20s' in batch
    foreign[0] = True
    with pytest.raises(RuntimeError, match='不属于面板'):
        firewall.apply([], [], 100)
    assert len(batches) == 1


def test_api_authentication_csrf_and_native_settings_without_fail2ban(guard, http_server, monkeypatch):
    engine, now, _ = guard
    monkeypatch.setattr(agent, '_ip_security', lambda: SimpleNamespace(guard=engine, observation={'ssh': {}}))
    assert http_server('GET', '/security/logins', authenticated=False)[0] == 401
    assert http_server('POST', '/security/logins', {'action': 'settings'}, authenticated=False)[0] == 401
    fail(engine, now)
    code, data = http_server('GET', '/security/logins')
    assert code == 200 and data['failure_24h'] == 5 and data['csrf_token']
    assert http_server('POST', '/security/logins', {**DEFAULTS, 'action': 'settings', 'enabled': False})[0] == 200
    assert not engine.blocked('8.8.8.8')
    for data in ({'action': 'settings', 'enabled': 'yes'}, {'action': 'settings', **DEFAULTS, 'maxretry': 0}, {'action': 'shell'}):
        assert http_server('POST', '/security/logins', data)[0] == 400


def test_manual_ban_api_uses_authenticated_scope_and_audits_target(guard, http_server, monkeypatch):
    engine, _, _ = guard
    audit = []
    monkeypatch.setattr(agent, '_ip_security', lambda: SimpleNamespace(guard=engine, observation={'ssh': {}}))
    monkeypatch.setattr(agent, '_audit_event', lambda action, **fields: audit.append((action, fields)))
    data = {'action': 'ban', 'ip': '8.8.8.8', 'source': 'panel'}
    assert http_server('POST', '/security/logins', data, authenticated=False)[0] == 401
    assert not engine.blocked('8.8.8.8')
    assert http_server('POST', '/security/logins', data)[0] == 200
    assert engine.blocked('8.8.8.8')
    assert audit[-1] == ('login_guard_ban', {'actor': '127.0.0.1', 'target': '8.8.8.8', 'detail': {'source': 'panel'}})
    assert http_server('POST', '/security/logins', {**data, 'ip': '127.0.0.1'})[0] == 400


def test_actual_login_endpoint_records_failures_enforces_ban_and_rejects_missing_csrf(guard, http_server, monkeypatch):
    engine, now, _ = guard
    monkeypatch.setattr(agent, '_ip_security', lambda: SimpleNamespace(guard=engine, observation={'ssh': {}}))
    monkeypatch.setattr(agent, '_request_client_ip', lambda _: '8.8.4.4')
    monkeypatch.setattr(agent, '_login_failures', {})
    host = urlsplit(http_server.origin)
    def post(path, body, headers=None):
        connection = http.client.HTTPConnection(host.hostname, host.port, timeout=5)
        connection.request('POST', path, body, headers or {'Content-Type': 'application/x-www-form-urlencoded'})
        response = connection.getresponse()
        code = response.status
        response.read()
        connection.close()
        return code
    for _ in range(5):
        now[0] += .1
        assert post('/login', 'password=wrong-password') == 401
    assert engine.blocked('8.8.4.4') and engine.status()['failure_24h'] == 5
    assert post('/login', 'password=test-password-123') == 429
    assert post('/security/logins', json.dumps({**DEFAULTS, 'action': 'settings', 'enabled': False}),
                {'Cookie': agent.COOKIE_NAME + '=' + http_server.cookie, 'Content-Type': 'application/json'}) == 403
    assert engine.status()['settings']['enabled']
