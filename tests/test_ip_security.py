import json
import re
import copy
import time

import pytest

import agent
import ip_security as security
import security_observation
from test_deployment_removal import http_server as http_server, runtime as runtime
from test_ssh_terminal import request


@pytest.fixture
def engine(tmp_path):
    home = tmp_path / 'data'
    home.mkdir()
    audit = home / 'audit.jsonl'
    audit.write_text('', encoding='utf-8')
    root = tmp_path / 'fail2ban'
    state = {'commands': [], 'installed': True, 'banned': {'sshd': {'198.51.100.20'}},
             'ignores': '127.0.0.0/8 ::1 203.0.113.10', 'validate': True, 'events': [],
             'ssh': {'ports': [2222], 'ready': True, 'backend': 'systemd', 'logpath': '', 'detail': '已识别 SSH'}}
    def which(name):
        return name if name in {'nft', 'apt-get', 'systemctl'} or name == 'fail2ban-client' and state['installed'] else None
    def run(command, **kwargs):
        state['commands'].append(command)
        if command[:3] == ['apt-get', 'install', '-y']:
            state['installed'] = True
        if command == ['fail2ban-client', '-t'] and not state['validate']:
            raise RuntimeError('invalid configuration')
        if command == ['fail2ban-client', 'status']:
            return 'Status\n|- Number of jail: 1\n`- Jail list: ' + ', '.join(state['banned'])
        if command[:2] == ['fail2ban-client', 'get']:
            if command[2:] == ['logtarget']:
                return 'STDOUT'
            jail, field = command[2:4]
            if field == 'actions':
                return 'The jail ' + jail + ' has the following actions:\nnftables-multiport'
            if field == 'banip':
                return ' '.join(sorted(state['banned'][jail]))
            if field == 'ignoreip':
                return 'These IP addresses/networks are ignored:\n|- 127.0.0.0/8\n`- ::1\n203.0.113.10'
            return {'bantime': '3600', 'maxretry': '5', 'findtime': '600'}[field]
        if command == ['fail2ban-client', 'version']:
            return state.get('version', '1.1.0')
        if command[:2] == ['fail2ban-client', 'set']:
            jail, operation, ip = command[2:5]
            if operation == 'banip':
                state['banned'][jail].add(ip)
            else:
                state['banned'][jail].discard(ip)
        if command[:3] == ['fail2ban-client', 'reload', '--if-exists']:
            jail = command[3]
            path = root / 'jail.d' / f'{jail}.local'
            if path.exists() and 'enabled = true' in path.read_text():
                state['banned'].setdefault(jail, set())
            else:
                state['banned'].pop(jail, None)
        return 'OK'
    class Inspector:
        def scan(self):
            return {'checked_at': time.time(), 'ssh': copy.deepcopy(state['ssh']), 'ports': [], 'warnings': [],
                    'host_readable': True, 'docker_readable': True}
        def run(self, args):
            return '{"nftables": []}'
    def logs(args):
        return ''
    manager = security.Manager(home, audit, config_root=root, runner=run, which=which, supported=True,
                               audit=lambda *args: state['events'].append(args), inspector=Inspector(),
                               history=security_observation.History(home, runner=logs, log_root=tmp_path / 'logs'))
    yield manager, state, root
    if manager.worker:
        manager.worker.join(5)
    if manager.scan_worker:
        manager.scan_worker.join(5)


def finish(manager, data, client='203.0.113.10'):
    manager.start({**data, 'confirmed': True}, client)
    manager.worker.join(timeout=5)
    assert not manager.worker.is_alive()
    if manager.scan_worker:
        manager.scan_worker.join(5)
    return manager.status(job_only=True)['job']


def test_reads_existing_rules_ipv4_ipv6_and_whitelist(engine):
    manager, _, _ = engine
    status = manager.status('sshd')
    assert status['running'] and status['selected']['banned'] == ['198.51.100.20']
    assert '::1/128' in status['selected']['ignoreip']
    with pytest.raises(ValueError):
        manager.status('nonexistent')


@pytest.mark.parametrize('ip', ['203.0.113.10', '::ffff:203.0.113.10', '127.0.0.2', '::1', '0.0.0.0', '224.0.0.1'])
def test_never_bans_current_client_or_special_addresses(engine, ip):
    manager, state, _ = engine
    with pytest.raises(ValueError):
        manager.start({'action': 'ban', 'confirmed': True, 'jail': 'sshd', 'ip': ip}, '203.0.113.10')
    assert not state['commands']


@pytest.mark.parametrize('changes', [{'jail': '--all'}, {'jail': 'sshd;reboot'}, {'ip': '1.2.3.4;id'},
                                     {'ip': '192.168.0.0/24'}, {'ip': 'example.com'}, {'confirmed': False}, {'action': 'flush'}])
def test_rejects_shell_and_unconfirmed_or_unbounded_operations(engine, changes):
    manager, state, _ = engine
    with pytest.raises(ValueError):
        manager.start({'action': 'ban', 'jail': 'sshd', 'ip': '198.51.100.33', 'confirmed': True, **changes}, '203.0.113.10')
    assert not state['commands']


def test_manual_ban_and_unban_verify_only_selected_rule(engine):
    manager, state, _ = engine
    assert finish(manager, {'action': 'ban', 'jail': 'sshd', 'ip': '2001:db8::4'})['state'] == 'succeeded'
    assert '2001:db8::4' in state['banned']['sshd']
    assert finish(manager, {'action': 'unban', 'jail': 'sshd', 'ip': '2001:db8::4'})['state'] == 'succeeded'
    assert '2001:db8::4' not in state['banned']['sshd']
    assert all('--all' not in command and '-F' not in command and 'stop' not in command for command in state['commands'])
    assert len(state['events']) == 2


def test_fail2ban_allowlisted_address_is_never_manually_banned(engine):
    manager, state, _ = engine
    assert finish(manager, {'action': 'ban', 'jail': 'sshd', 'ip': '203.0.113.10'}, client='203.0.113.99')['state'] == 'failed'
    assert not any(command[:2] == ['fail2ban-client', 'set'] for command in state['commands'])


@pytest.mark.parametrize('text', ['trusted-host', 'example.com', 'unknown output'])
def test_unresolved_whitelist_is_not_silently_ignored(text):
    with pytest.raises(ValueError, match='无法核实'):
        security.networks(text)


def test_install_configure_adds_only_owned_jail_with_backup_and_admin_whitelist(engine):
    manager, state, root = engine
    state['installed'] = False
    root.mkdir()
    business = root / 'jail.local'
    business.write_text('[sshd]\nport = 2222\n')
    result = finish(manager, {'action': 'configure', 'ports': [6868, 80, 443]})
    assert result['state'] == 'succeeded'
    assert business.read_text() == '[sshd]\nport = 2222\n'
    text = (root / 'jail.d/mini-deploy-panel.local').read_text()
    assert '203.0.113.10' in text and 'port = 6868,80,443' in text
    assert 'banaction = nftables-multiport' in text
    assert ['apt-get', 'install', '-y', 'fail2ban'] in state['commands']
    assert ['fail2ban-client', 'reload', '--if-exists', security.JAIL] in state['commands']
    assert not any(command[:2] == ['systemctl', 'restart'] for command in state['commands'])
    assert manager.status()['panel_enabled'] is True
    assert finish(manager, {'action': 'configure'})['state'] == 'succeeded'
    assert list(manager.home.glob('ip-security-backups/*/mini-deploy-panel.local'))


def test_invalid_config_keeps_files_disabled_without_erasing_external_rules(engine):
    manager, state, root = engine
    state['validate'] = False
    assert finish(manager, {'action': 'configure'})['state'] == 'failed'
    assert 'enabled = false' in (root / 'jail.d/mini-deploy-panel.local').read_text()
    assert (root / 'filter.d/mini-deploy-panel.conf').exists()
    assert not any(command[:3] == ['systemctl', 'enable', '--now'] for command in state['commands'])


def test_foreign_configuration_with_same_filename_is_never_overwritten(engine):
    manager, _, root = engine
    (root / 'jail.d').mkdir(parents=True)
    path = root / 'jail.d/mini-deploy-panel.local'
    path.write_text('user-owned settings')
    assert finish(manager, {'action': 'configure'})['state'] == 'failed'
    assert path.read_text() == 'user-owned settings'


def test_pause_preserves_configs_backups_and_other_rules(engine):
    manager, state, root = engine
    assert finish(manager, {'action': 'configure'})['state'] == 'succeeded'
    assert finish(manager, {'action': 'disable'})['state'] == 'succeeded'
    assert 'sshd' in state['banned'] and security.JAIL not in state['banned']
    assert 'enabled = false' in (root / 'jail.d/mini-deploy-panel.local').read_text()
    assert (root / 'filter.d/mini-deploy-panel.conf').exists()
    assert list(manager.home.glob('ip-security-backups/pause-*/mini-deploy-panel.local'))


@pytest.mark.parametrize('data', [{'ignoreip': ['0.0.0.0/0']}, {'ignoreip': ['fe80::1%eth0']}, {'maxretry': True},
                                {'ports': [22]}, {'bantime': -1}, {'ignoreip': ['1.2.3.4\nenabled=true']}])
def test_settings_reject_injected_or_unsafe_values(engine, data):
    manager, state, _ = engine
    with pytest.raises(ValueError):
        manager.start({'action': 'configure', 'confirmed': True, **data}, '203.0.113.10')
    assert not state['commands']


def test_filter_ignores_success_arbitrary_actions_and_injected_fields():
    # Date extraction in Fail2ban removes the matched date; the expression accepts that empty slot.
    template = security.FILTER.split('failregex = ', 1)[1].splitlines()[0]
    pattern = re.compile(template.replace('<ADDR>', r'(?P<ip>[0-9a-fA-F:.]+)'))
    base = {'at': '', 'action': 'login', 'actor': '198.51.100.33', 'target': '', 'success': False, 'detail': {'reason': 'bad_password'}}
    def encode(item):
        return json.dumps(item, separators=(',', ':'))
    assert pattern.fullmatch(encode(base)).group('ip') == '198.51.100.33'
    assert pattern.fullmatch(encode({**base, 'actor': '2001:db8::2'}))
    for changes in [{'success': True}, {'action': 'ip_security_requested'}, {'target': 'something'}, {'detail': {'reason': 'other'}}]:
        assert not pattern.fullmatch(encode({**base, **changes}))


@pytest.mark.parametrize('endpoint', ['/security/center', '/security/fail2ban'])
def test_routes_require_auth_csrf_and_return_async_job(http_server, runtime, monkeypatch, engine, endpoint):
    manager, _, _ = engine
    monkeypatch.setattr(agent, '_ip_security', lambda: manager)
    data = {'action': 'ban', 'confirmed': True, 'jail': 'sshd', 'ip': '198.51.100.90'}
    assert request(http_server, 'GET', endpoint, authenticated=False)[0] == 401
    assert request(http_server, 'POST', endpoint, data, authenticated=False)[0] == 401
    assert request(http_server, 'POST', endpoint, data, csrf=False)[0] == 403
    assert request(http_server, 'GET', endpoint + '?jail=sshd')[1]['selected']['name'] == 'sshd'
    assert request(http_server, 'POST', endpoint, data)[0] == 202
    manager.worker.join(5)
    assert request(http_server, 'GET', endpoint + '?job=1')[1]['job']['state'] == 'succeeded'


def test_ssh_custom_port_and_systemd_journal_preserve_server_configuration(engine):
    manager, state, root = engine
    state['banned'].pop('sshd')
    result = finish(manager, {'action': 'configure_ssh', 'expected_ssh': security.ssh_fingerprint(state['ssh']), 'increment': True})
    assert result['state'] == 'succeeded'
    content = (root / 'jail.d/mini-deploy-sshd.local').read_text()
    assert 'port = 2222' in content and 'backend = systemd' in content
    assert 'logpath' not in content and 'bantime.increment = true' in content
    assert '203.0.113.10' in content
    assert not any(command[:2] == ['systemctl', 'restart'] for command in state['commands'])
    assert manager.status()['ssh_enabled']
    assert finish(manager, {'action': 'disable_ssh'})['state'] == 'succeeded'
    assert 'enabled = false' in (root / 'jail.d/mini-deploy-sshd.local').read_text()


def test_existing_ssh_rule_is_never_overwritten_or_duplicated(engine):
    manager, state, root = engine
    result = finish(manager, {'action': 'configure_ssh', 'expected_ssh': security.ssh_fingerprint(state['ssh'])})
    assert result['state'] == 'failed' and '已有 sshd' in result['message']
    assert not (root / 'jail.d/mini-deploy-sshd.local').exists()
    assert 'sshd' in state['banned']


def test_ssh_scan_change_requires_a_new_preview(engine):
    manager, state, root = engine
    before = security.ssh_fingerprint(state['ssh'])
    state['ssh']['ports'] = [2200]
    result = finish(manager, {'action': 'configure_ssh', 'expected_ssh': before})
    assert result['state'] == 'failed' and '已变化' in result['message']
    assert not root.exists()


def test_ssh_unconfirmed_source_does_not_install_or_write(engine):
    manager, state, root = engine
    state['ssh']['ready'] = False
    assert finish(manager, {'action': 'configure_ssh', 'expected_ssh': security.ssh_fingerprint(state['ssh'])})['state'] == 'failed'
    assert not root.exists()


def test_failed_ssh_configuration_stays_disabled_and_preserves_panel_rule(engine):
    manager, state, root = engine
    assert finish(manager, {'action': 'configure'})['state'] == 'succeeded'
    panel = (root / 'jail.d/mini-deploy-panel.local').read_bytes()
    state['banned'].pop('sshd')
    state['validate'] = False
    assert finish(manager, {'action': 'configure_ssh', 'expected_ssh': security.ssh_fingerprint(state['ssh'])})['state'] == 'failed'
    assert (root / 'jail.d/mini-deploy-panel.local').read_bytes() == panel
    assert 'enabled = false' in (root / 'jail.d/mini-deploy-sshd.local').read_text()


def test_increment_rejects_old_fail2ban_before_writing(engine):
    manager, state, root = engine
    state['version'] = '0.10.6'
    assert finish(manager, {'action': 'configure', 'increment': True})['state'] == 'failed'
    assert not root.exists()


def test_background_scan_reports_unknown_firewall_without_claiming_enforcement(engine):
    manager, _, _ = engine
    manager.scan()
    data = manager.status()['observation']
    assert data['ssh']['ports'] == [2222] and data['ssh']['fingerprint']
    assert next(item for item in data['checks'] if item['name'] == '防火墙对象')['state'] == 'unknown'


def test_security_alert_uses_existing_notification_mute_and_retry(engine, tmp_path):
    import monitoring
    now = time.time()
    monitor = monitoring.Monitor(tmp_path / 'monitor.json', clock=lambda: now)
    security.evaluate(monitor, {'history': {'readable': True, 'checked_at': now, 'recent_bans': 7, 'recent_ips': 5}})
    assert monitor.snapshot()['alerts'][0]['source'] == 'security'
    monitor.muted_until = now + 3600
    assert monitor.next_notification(True) is None
    monitor.muted_until = 0
    assert monitor.next_notification(True)[0] == 'security:ban-burst'
    security.evaluate(monitor, {'history': {'readable': False, 'checked_at': now, 'recent_bans': 0}})
    assert monitor.snapshot()['alerts'][0]['stale']
    security.evaluate(monitor, {'history': {'readable': True, 'checked_at': now, 'recent_bans': 0}})
    assert not monitor.snapshot()['alerts']
