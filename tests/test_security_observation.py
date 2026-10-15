"""Host discovery never mutates services; event collection survives restarts."""
from datetime import datetime
import json
import time

import pytest

import security_observation as security


def native_line(kind='Ban', ip='198.51.100.40', jail='sshd', at=None, fraction='001'):
    at = at or time.time() - 10
    date = datetime.fromtimestamp(at).strftime('%Y-%m-%d %H:%M:%S')
    return f'{date},{fraction} fail2ban.actions [12]: NOTICE [{jail}] {kind} {ip}\n'


def test_inspector_detects_custom_ssh_port_and_mapped_docker_database(tmp_path):
    calls = []
    def run(args):
        calls.append(args)
        if args[0] == 'ss':
            return ('LISTEN 0 128 0.0.0.0:2200 0.0.0.0:* users:(("sshd",pid=12,fd=3))\n'
                    'LISTEN 0 128 [::]:2200 [::]:* users:(("sshd",pid=12,fd=4))\n'
                    'LISTEN 0 128 127.0.0.1:6379 0.0.0.0:* users:(("redis",pid=13,fd=3))')
        if args[0] == 'journalctl':
            return json.dumps({'_COMM': 'sshd-session', 'MESSAGE': 'not persisted'})
        if args[0] == 'docker':
            return json.dumps({'Names': 'db', 'Ports': '0.0.0.0:15432->5432/tcp, [::]:15432->5432/tcp'})
        pytest.fail(str(args))
    facts = security.Inspector(runner=run, which=lambda _: True, log_root=tmp_path).scan()
    assert facts['ssh']['ports'] == [2200] and facts['ssh']['backend'] == 'systemd'
    assert len(facts['ports']) == 2
    assert all(row['service'] == 'PostgreSQL' and row['port'] == 15432 for row in facts['ports'])
    assert not any('iptables' in call or 'systemctl' in call for call in calls)


def test_listener_failure_is_unknown_and_never_defaults_to_port22(tmp_path):
    def fail(args):
        raise RuntimeError('no permissions')
    facts = security.Inspector(runner=fail, which=lambda _: True, log_root=tmp_path).scan()
    assert not facts['ssh']['ready'] and facts['ssh']['ports'] == []
    assert not facts['host_readable'] and not facts['docker_readable'] and len(facts['warnings']) == 2


def test_docker_ranges_and_mapped_loopback_are_handled_without_expansion():
    rows = security.docker_ports(json.dumps({'Names': 'range', 'Ports': '0.0.0.0:15000-15010->5430-5440/tcp'}))
    assert len(rows) == 1 and rows[0]['port'] == 15002 and rows[0]['container_port'] == 5432
    assert security.endpoint('[::ffff:127.0.0.1]:3306')[2] == 'loopback'
    with pytest.raises(ValueError):
        security.docker_ports(json.dumps({'Ports': '0.0.0.0:15000-15010->5432/tcp'}))


def test_ssh_auth_log_fallback_and_no_log_is_not_ready(tmp_path):
    def run(args):
        return 'LISTEN 0 128 192.168.1.3:2222 0.0.0.0:* users:(("sshd",pid=42,fd=3))'
    scanner = security.Inspector(runner=run, which=lambda _: None, log_root=tmp_path)
    assert not scanner.scan()['ssh']['ready']
    (tmp_path / 'auth.log').write_text('Oct 15 10:00:00 host sshd[42]: Server listening on port 2222.\n')
    ssh = scanner.scan()['ssh']
    assert ssh['ready'] and ssh['backend'] == 'polling' and ssh['ports'] == [2222]


def test_persistent_file_history_incremental_partial_lines_and_deduplication(tmp_path):
    log = tmp_path / 'fail2ban.log'
    original = native_line('Found', fraction='001') + native_line('Found', fraction='002') + native_line(fraction='003')
    log.write_text(original, encoding='utf-8')
    history = security.History(tmp_path / 'home', log_root=tmp_path)
    assert history.collect()['readable']
    first = history.snapshot()
    assert first['total'] == 1 and first['items'][0]['observed_failures_10m'] == 2
    assert history.collect()['readable'] and history.snapshot()['total'] == 1
    partial = native_line('Unban', fraction='004')
    with log.open('a') as stream:
        stream.write(partial[:-1])
    history.collect()
    assert history.snapshot()['total'] == 1
    with log.open('a') as stream:
        stream.write('\n')
    restarted = security.History(tmp_path / 'home', log_root=tmp_path)
    restarted.collect()
    assert restarted.snapshot()['total'] == 2
    assert log.read_text() == original + partial
    assert set(restarted.snapshot()['items'][0]) == {'id', 'ts', 'jail', 'kind', 'ip', 'reason', 'observed_failures_10m'}


def test_rotated_or_truncated_logs_keep_collected_history_and_report_gap(tmp_path):
    log = tmp_path / 'fail2ban.log'
    log.write_text(native_line())
    history = security.History(tmp_path / 'home', log_root=tmp_path)
    history.collect()
    log.rename(tmp_path / 'fail2ban.log.1')
    log.write_text(native_line('Unban'))
    result = history.collect()
    assert '可能存在采集空档' in result['detail']
    assert history.snapshot()['total'] == 2
    assert (tmp_path / 'fail2ban.log.1').exists()


def test_file_tail_is_bounded_and_does_not_read_business_paths(tmp_path):
    business = tmp_path / 'business.txt'
    business.write_text('private content')
    root = tmp_path / 'logs'
    root.mkdir()
    args_seen = []
    def run(args):
        args_seen.append(args)
        return ''
    history = security.History(tmp_path / 'home', runner=run, log_root=root)
    history.collect(str(root / '..' / 'business.txt'))
    assert args_seen and history.snapshot()['total'] == 0
    assert business.read_text() == 'private content'
    (root / 'fail2ban.log').write_text('x' * (security.LIMIT * 2) + '\n' + native_line())
    history.collect()
    assert history.snapshot()['total'] == 1


def test_journal_collection_persists_with_time_and_source_limits(tmp_path):
    now = time.time()
    def run(args):
        assert '--since' in args and '-n' in args
        return json.dumps({'MESSAGE': native_line(), '__REALTIME_TIMESTAMP': str(int(now * 1e6))})
    history = security.History(tmp_path / 'home', runner=run, log_root=tmp_path / 'missing')
    history.collect('STDOUT')
    assert history.snapshot()['total'] == 1
    security.History(tmp_path / 'home', runner=run, log_root=tmp_path / 'missing').collect('STDOUT')
    assert history.snapshot()['total'] == 1
    def fail(args):
        raise RuntimeError('journal not readable')
    history.run = fail
    assert not history.collect()['readable']
    assert history.snapshot()['total'] == 1


def test_oversized_appended_line_makes_progress_and_does_not_parse_embedded_events(tmp_path):
    log = tmp_path / 'fail2ban.log'
    log.write_text(native_line())
    history = security.History(tmp_path / 'home', log_root=tmp_path)
    history.collect()
    with log.open('a') as stream:
        stream.write('x' * security.LIMIT + native_line(ip='198.51.100.77') + native_line('Unban'))
    history.collect()
    assert history.snapshot()['total'] == 1
    history.collect()
    result = history.snapshot()
    assert result['total'] == 2 and not any(item['ip'] == '198.51.100.77' for item in result['items'])


def test_history_pagination_retention_and_expiry_use_actual_ban_duration(tmp_path):
    log = tmp_path / 'fail2ban.log'
    log.write_text(''.join(native_line(ip=f'198.51.100.{n}', fraction=f'{n:03}') for n in range(1, 62)) +
                   native_line(ip='198.51.100.99', at=time.time() - security.RETENTION - 1000))
    history = security.History(tmp_path / 'home', log_root=tmp_path)
    history.collect()
    assert history.snapshot()['total'] == 61
    assert len(history.snapshot()['items']) == 50 and len(history.snapshot(1)['items']) == 11
    with pytest.raises(ValueError):
        history.snapshot(-1)
    values = security.timed_bans('198.51.100.2 \t2026-10-15 10:00:00 + 7200 = 2026-10-15 12:00:00\n'
                                 '2001:db8::4 \t2026-10-15 10:00:00 + -1 = 9999-12-31 23:59:59\n')
    assert values['198.51.100.2']['expires_at'] - values['198.51.100.2']['started_at'] == 7200
    assert values['2001:db8::4']['permanent'] and values['2001:db8::4']['expires_at'] is None


@pytest.mark.parametrize('message', ['invalid', '[sshd] Ban example.com', '[sshd] Ban 198.51.100.2; rm /tmp/x'])
def test_invalid_or_unstructured_events_are_not_stored(message):
    assert security.parse_event(message, time.time()) is None
