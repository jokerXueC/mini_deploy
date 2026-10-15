"""Security facts must remain read-only, bounded, durable and explicit about missing evidence."""
from datetime import datetime
import json
import os
import stat
import time

import pytest

from security_center import Center, login_event
from security_observation import History


@pytest.fixture
def center(tmp_path):
    now = time.time()
    root = tmp_path / 'etc'
    (root / 'ssh').mkdir(parents=True)
    (root / 'ssh/sshd_config').write_text('PasswordAuthentication yes\n')
    state = {'calls': [], 'journal': [], 'docker': [], 'fail': set(), 'now': now}
    def run(args):
        state['calls'].append(args)
        if args[0] in state['fail']:
            raise RuntimeError('unavailable')
        if args[0] == 'sshd':
            return 'passwordauthentication yes\npermitrootlogin prohibit-password\npermitemptypasswords no\n'
        if args[:3] == ['docker', 'ps', '-q']:
            return '\n'.join(item['id'] for item in state['docker'])
        if args[:2] == ['docker', 'inspect']:
            return '\n'.join(json.dumps(item) for item in state['docker'] if item['id'] in args)
        if args[0] == 'journalctl':
            return '\n'.join(json.dumps(item) for item in state['journal'])
        pytest.fail(str(args))
    history = History(tmp_path / 'data', runner=run, log_root=tmp_path / 'logs', clock=lambda: state['now'])
    scanner = Center(history, runner=run, which=lambda name: name, config_root=root, clock=lambda: state['now'])
    return scanner, state, root


def facts(backend='systemd', **kwargs):
    return {'ssh': {'backend': backend, **kwargs}, 'ports': []}


def journal(state, message, number=0):
    return {'MESSAGE': message, '__REALTIME_TIMESTAMP': str(int((state['now'] - 5 + number / 1000) * 1e6)), '__CURSOR': f'cursor{number}'}


@pytest.mark.parametrize('message,kind,account,ip', [
    ('Accepted publickey for deploy from 192.0.2.8 port 45200 ssh2: ED25519 SHA256:private', 'login_success', 'deploy', '192.0.2.8'),
    ('Failed password for invalid user root from 2001:db8::42 port 42 ssh2', 'login_failure', 'root', '2001:db8::42'),
    ('Oct 15 10:00:00 host sshd-session[42]: Accepted keyboard-interactive/pam for user from 198.51.100.2 port 42 ssh2', 'login_success', 'user', '198.51.100.2')])
def test_parses_ssh_primary_authentication_without_saving_raw_messages(message, kind, account, ip):
    result = login_event(message, time.time() - 1)
    assert result['kind'] == kind and result['account'] == account and result['ip'] == ip
    assert 'SHA256' not in json.dumps(result)


@pytest.mark.parametrize('message', ['Invalid user root from 192.0.2.8', 'Failed password for root from example.com port 22',
                                     'Accepted publickey for user from 192.0.2.8 port 22\nmalformed', 'unrelated line'])
def test_rejects_unstructured_or_injected_login_messages(message):
    assert login_event(message, time.time()) is None


def test_syslog_year_rollover_and_unparseable_time_are_not_replaced_with_now():
    now = datetime(2027, 1, 1, 1).timestamp()
    event = login_event('Dec 31 23:59:00 host sshd[1]: Failed password for root from 192.0.2.8 port 42 ssh2', now=now)
    assert datetime.fromtimestamp(event['ts']).year == 2026
    assert login_event('host sshd[1]: Failed password for root from 192.0.2.8 port 42 ssh2', now=now) is None


def test_ssh_checks_and_config_baseline_detect_change_without_storing_content(center):
    scanner, state, root = center
    original = (root / 'ssh/sshd_config').read_text()
    first = scanner.scan(facts())
    assert any(item['id'] == 'ssh:passwordauthentication' for item in first['risks'])
    assert not any('permitrootlogin' in item['id'] for item in first['risks'])
    assert scanner.activity()['total'] == 0
    (root / 'ssh/sshd_config').write_text('PasswordAuthentication no\n# SECRET-EXAMPLE\n')
    state['now'] += 1
    scanner.scan(facts())
    activity = scanner.activity()
    assert activity['config_changes_24h'] == 1 and activity['items'][0]['kind'] == 'config_change'
    scanner.scan(facts())
    assert scanner.activity()['total'] == 1
    assert 'SECRET-EXAMPLE' not in scanner.history.path.read_bytes().decode('utf-8', 'ignore')
    assert original != (root / 'ssh/sshd_config').read_text()
    assert not any(args[0] in {'systemctl', 'chmod', 'rm'} for args in state['calls'])


def test_docker_privileges_socket_and_mapped_ports_are_reported_with_evidence(center):
    scanner, state, _ = center
    state['docker'] = [{'id':'a' * 64, 'name':'/manager', 'privileged':True, 'network':'host', 'pid':'host',
        'capabilities':['SYS_ADMIN'], 'mounts':[{'Source':'/var/run/docker.sock','Destination':'/run/docker.sock','RW':False},
                                               {'Source':'/etc','Destination':'/host/etc','RW':True}]}]
    data = facts()
    data['ports'] = [{'kind':'docker','host':'0.0.0.0','port':15432,'container_port':5432,'protocol':'tcp',
                      'container':'db','service':'PostgreSQL','detail':'需要核对外部路由'}]
    result = scanner.scan(data)
    assert len(result['risks']) == 8
    assert any('只读' in item['detail'] for item in result['risks'] if ':socket:' in item['id'])
    inspect = next(args for args in state['calls'] if args[:2] == ['docker', 'inspect'])
    assert '.Env' not in inspect[3] and '.HostConfig' in inspect[3]


def test_unreadable_scans_and_missing_files_never_resolve_to_safe(center):
    scanner, state, root = center
    scanner.scan(facts())
    (root / 'ssh/sshd_config').rename(root / 'ssh/preserved.config')
    state['fail'] = {'sshd', 'docker', 'journalctl'}
    result = scanner.scan(facts())
    assert any(item['state'] == 'unknown' and 'Docker' in item['name'] for item in result['checks'])
    assert any('当前不可读' in item['detail'] for item in result['checks'])
    assert not scanner.activity()['auth']['readable']
    assert (root / 'ssh/preserved.config').exists()


def test_journal_login_history_deduplicates_survives_restart_filters_and_paginates(center):
    scanner, state, root = center
    state['journal'] = [journal(state, f'Failed password for root from 198.51.100.{n} port 42 ssh2', n) for n in range(1, 62)]
    state['journal'].append(journal(state, 'Accepted publickey for deploy from 192.0.2.8 port 42 ssh2', 80))
    scanner.scan(facts())
    scanner.scan(facts())
    result = scanner.activity()
    assert result['total'] == 62 and len(result['items']) == 50 and result['login_failure_24h'] == 61
    assert len(scanner.activity(1, 'ssh')['items']) == 12 and scanner.activity(0, 'config')['total'] == 0
    restored = Center(scanner.history, runner=scanner.run, which=scanner.which, config_root=root, clock=scanner.clock)
    assert restored.activity()['total'] == 62
    state['fail'].add('journalctl')
    restored.scan(facts())
    assert restored.activity()['total'] == 62 and not restored.activity()['auth']['readable']


def test_incremental_file_logins_keep_partial_lines_and_read_only_snapshot(center):
    scanner, state, _ = center
    assert scanner.activity()['total'] == 0 and not scanner.history.path.exists()
    scanner.history.log_root.mkdir()
    log = scanner.history.log_root / 'auth.log'
    date = datetime.fromtimestamp(state['now'] - 3).isoformat(timespec='seconds')
    line = date + ' host sshd[42]: Accepted publickey for user from 192.0.2.8 port 42 ssh2'
    log.write_text(line)
    scanner.scan(facts('polling', logpath=str(log)))
    assert scanner.activity()['total'] == 0
    with log.open('a') as stream:
        stream.write('\n')
    scanner.scan(facts('polling', logpath=str(log)))
    assert scanner.activity()['login_success_24h'] == 1 and log.read_text().strip() == line
    scanner.scan(facts('polling', logpath=str(log.parent / '../private.log')))
    assert scanner.activity()['total'] == 1 and not scanner.activity()['auth']['readable']


def test_unified_activity_contains_fail2ban_and_never_erases_legacy_events(center):
    scanner, state, _ = center
    scanner.scan(facts())
    with scanner.history.connection() as db:
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)', ('legacy', state['now'] - 10, 'sshd', 'ban', '192.0.2.4', 'Fail2ban 封禁'))
    assert scanner.activity()['total'] == 1 and scanner.activity(0, 'fail2ban')['items'][0]['target'] == 'sshd'
    assert scanner.history.snapshot()['total'] == 1
    with pytest.raises(ValueError):
        scanner.activity(0, 'invalid')


def test_docker_bind_config_discovery_uses_only_recognized_targets_and_common_roots(center):
    scanner, state, _ = center
    from pathlib import Path
    state['docker'] = [{'id':'b' * 64, 'name':'/caddy', 'privileged':False, 'network':'bridge', 'pid':'', 'capabilities':[],
        'mounts':[{'Type':'bind','Source':'/opt/site/Caddyfile','Destination':'/etc/caddy/Caddyfile','RW':True},
                  {'Type':'bind','Source':'/opt/site/nginx','Destination':'/etc/nginx','RW':False},
                  {'Type':'bind','Source':'/opt/../private','Destination':'/etc/caddy/Caddyfile','RW':True},
                  {'Type':'bind','Source':'/data/secret','Destination':'/srv/secret','RW':True}]}]
    _, _, paths = scanner._docker()
    assert paths == [Path('/opt/site/Caddyfile'), Path('/opt/site/nginx/nginx.conf')]


def test_concentrated_failures_alert_obeys_unknown_and_partial_recovery(center, tmp_path):
    import ip_security
    import monitoring
    scanner, state, _ = center
    state['journal'] = [journal(state, 'Failed password for root from 198.51.100.42 port 42 ssh2', n) for n in range(12)]
    scanner.scan(facts())
    activity = scanner.activity()
    assert activity['concentrated_failures'] == [{'ip':'198.51.100.42','attempts':12}]
    monitor = monitoring.Monitor(tmp_path / 'monitor.json', clock=scanner.clock)
    ip_security.evaluate(monitor, {'activity': activity})
    assert monitor.snapshot()['alerts'][0]['id'] == 'security:ssh-failures'
    for auth in [{'readable':False}, {'readable':True,'checked_at':state['now'],'limited':True}]:
        ip_security.evaluate(monitor, {'activity': {'auth':auth,'concentrated_failures':[]}})
        assert monitor.snapshot()['alerts'][0]['active']


@pytest.mark.skipif(os.name != 'posix', reason='POSIX permission bits required')
def test_writable_config_is_reported_without_chmod(center):
    scanner, _, root = center
    path = root / 'ssh/sshd_config'
    path.chmod(0o666)
    result = scanner.scan(facts())
    assert any(item['id'] == 'file:' + str(path) for item in result['risks'])
    assert stat.S_IMODE(path.stat().st_mode) == 0o666
