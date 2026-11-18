"""Scheduled summaries: accurate windows, restart/retry semantics and authenticated settings."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

import agent
import notification_digest as digest
from test_deployment_removal import runtime, http_server
from test_observation_analysis import histories, NOW
from test_ssh_terminal import request


@pytest.fixture
def store(tmp_path):
    now = [1000.0]
    result = digest.Store(tmp_path, clock=lambda: now[0])
    result.now = now
    return result


def config(interval=15, **channels):
    return {'digest': {'enabled': True, 'interval_minutes': interval},
            'wecom': {'enabled': True, 'webhook_url': 'https://example.test/secret'}, **channels}


def report(now=0, body='完整统计报告'):
    return {'since': now - 900, 'until': now, 'body': body}


def publish(store, cfg=None, body='完整统计报告'):
    cfg = cfg or config()
    ticket = store.sync(cfg)
    store.now[0] = ticket['next_at']
    store.publish(report(store.now[0], body), cfg, ticket)
    return cfg


@pytest.mark.parametrize('value', [0, -1, 1441, True, 1.5, '15', float('inf')])
def test_invalid_intervals(value):
    with pytest.raises(ValueError):
        digest.settings({'enabled': True, 'interval_minutes': value})


def test_config_compatibility_preservation_and_validation():
    defaults = agent._default_notification_config()
    assert defaults['digest'] == {'enabled': False, 'interval_minutes': 15}
    previous = agent._notification_config_from_raw(config(30))
    assert agent._notification_config_from_raw({'email': {}}, previous)['digest'] == config(30)['digest']
    with pytest.raises(ValueError):
        agent._notification_config_from_raw({'digest': {'enabled': 'yes'}})


def test_fixed_15_complete_minutes_and_real_statistics(histories):
    server, docker, traffic = histories
    events = [{'at_ts': NOW + t, 'title': f'event {t}', 'kind': kind}
              for t, kind in [(-1, 'alert'), (1, 'alert'), (2, 'recovery'), (900, 'alert')]]
    result = digest.collect(lambda: server, lambda: docker, lambda: traffic,
                            lambda: SimpleNamespace(snapshot=lambda: {'events': events, 'alerts': []}),
                            '测试主机', NOW + 959)
    assert (result['since'], result['until']) == (NOW, NOW + 900)
    body = result['body']
    assert '平均 33.33%' in body
    assert '已采集请求：4' in body
    assert '5xx：1' in body
    assert '新增异常 1；恢复 1' in body
    assert 'event -1' not in body and 'event 900' not in body
    assert '采集进度落后' in body and '覆盖' in body


def test_source_failure_does_not_erase_other_sources_or_claim_healthy(histories):
    def broken():
        raise sqlite3.OperationalError('unavailable')
    result = digest.collect(broken, lambda: histories[1], lambda: histories[2], broken, 'host', NOW + 900)
    assert '主机资源：来源暂不可读' in result['body']
    assert '异常：来源暂不可读' in result['body']
    assert '已采集请求：4' in result['body']
    assert '不能判断资源是否正常' in result['body']


def test_persistent_schedule_skip_missed_intervals_and_no_browser_needed(store):
    cfg, builds, sends = config(), [], []
    worker = digest.Worker(store, lambda: cfg, lambda now: builds.append(now) or report(now),
                           lambda *args: sends.append(args))
    worker.step()
    due = store.snapshot(cfg)['next_at']
    assert due == 1900 and not builds
    restored = digest.Store(store.path.parent.parent, clock=store.clock)
    assert restored.sync(cfg)['next_at'] == due
    store.now[0] += 5000
    worker.step()
    assert builds == [6000] and len(sends) == 1
    assert store.snapshot(cfg)['next_at'] == 6900
    worker.step()
    assert len(builds) == 1 and len(sends) == 1
    cfg['digest']['interval_minutes'] = 30
    assert store.sync(cfg)['next_at'] == 7800


def test_disabled_no_channels_and_disable_during_generation(store):
    cfg = config()
    calls = []
    worker = digest.Worker(store, lambda: cfg, lambda now: calls.append(now) or report(now), lambda *a: calls.append(a))
    cfg['digest']['enabled'] = False
    worker.step()
    store.now[0] += 10000
    worker.step()
    assert calls == []
    cfg['digest']['enabled'] = True
    cfg['wecom']['enabled'] = False
    worker.step()
    store.now[0] += 1000
    worker.step()
    assert calls == [] and not store.snapshot(cfg)['channels_ready']
    cfg['wecom']['enabled'] = True
    def disable(now):
        cfg['digest']['enabled'] = False
        return report(now)
    worker.collect = disable
    worker.step()
    assert store.snapshot(cfg)['last'] is None


def test_failed_part_independent_channels_and_restart(store):
    cfg = publish(store, config(email={'enabled': True}), '中文🙂' * 3000)
    first = store.next_delivery(cfg)
    store.receipt(first, True)
    store.now[0] += 4
    second = store.next_delivery(cfg)
    assert second['part'] == 2
    store.receipt(second, False)
    email = store.next_delivery(cfg)
    assert email['channel'] == 'email' and email['part'] == 1
    store.receipt(email, True)
    store.now[0] += 61
    assert store.next_delivery(cfg)['part'] == 2
    store.restart()
    assert store.next_delivery(cfg) is None
    store.now[0] += 11
    assert store.next_delivery(cfg)['part'] == 2
    store.receipt(second, True)
    store.now[0] += 4
    assert store.next_delivery(cfg)['part'] == 3
    assert 'secret' not in json.dumps(store.snapshot(cfg))


def test_full_utf8_transport_body_and_channel_change(store):
    body = '中文🙂\n' * 5000
    cfg = publish(store, body=body)
    with store.connect() as db:
        parts = list(db.execute('SELECT body FROM deliveries ORDER BY part'))
    assert ''.join(row[0].split('\n\n', 1)[1] for row in parts) == body
    assert all(len((TITLE_PREFIX + row[0]).encode()) < 4096 for row in parts)
    first = store.next_delivery(cfg)
    store.receipt(first, True)
    cfg['wecom']['webhook_url'] = 'https://example.test/new-destination'
    store.sync(cfg)
    assert store.next_delivery(cfg) is None
    status = store.snapshot(cfg)
    assert status['pending_reports'] == 0
    assert any(p['status'] == 'cancelled' and p['destination_changed'] for p in status['deliveries'])


TITLE_PREFIX = digest.TITLE + '\n\n'


def test_generation_failure_retry_and_successful_channel_not_resent(store):
    cfg = config(email={'enabled': True})
    attempts = []
    def broken(_now):
        raise OSError('private path')
    def send(channel, *_args):
        attempts.append(channel)
        if channel == 'wecom':
            raise RuntimeError('webhook secret')
    worker = digest.Worker(store, lambda: cfg, broken, send)
    worker.step()
    store.now[0] += 900
    worker.step()
    assert '生成失败' in store.snapshot(cfg)['error']
    worker.collect = lambda now: report(now)
    store.now[0] += 61
    worker.step()
    worker.step()
    assert attempts == ['wecom', 'email']
    store.now[0] += 61
    worker.step()
    assert attempts == ['wecom', 'email', 'wecom']
    assert store.snapshot(cfg)['error'] == ''


def test_completed_retention_and_pending_backpressure(store):
    cfg = config(1)
    for index in range(110):
        publish(store, cfg)
        store.receipt(store.next_delivery(cfg), True)
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM reports').fetchone()[0] <= 101
    for _ in range(101):
        publish(store, cfg)
    assert store.snapshot(cfg)['pending_reports'] == 100
    assert '暂停生成' in store.snapshot(cfg)['error']


def test_api_save_preview_and_authentication(http_server, runtime, monkeypatch):
    for method, path in [('GET', '/notifications'), ('POST', '/notifications'),
                         ('GET', '/notifications/digest/preview')]:
        assert http_server(method, path, authenticated=False)[0] == 401
    assert request(http_server, 'POST', '/notifications', config(), csrf=False)[0] == 403
    monkeypatch.setattr(agent, '_collect_notification_digest', lambda: report(1000))
    code, payload = http_server('GET', '/notifications/digest/preview')
    assert code == 200 and payload['body'] == '完整统计报告'
    assert http_server('POST', '/notifications', {'digest': {'enabled': True, 'interval_minutes': 0}})[0] == 400
    assert http_server('POST', '/notifications/test', {'digest': {'enabled': 'yes'}})[0] == 400
    code, saved = http_server('POST', '/notifications', config(30))
    assert code == 200 and saved['notifications']['digest'] == config(30)['digest']
    assert saved['digest']['next_at'] > 0 and saved['digest']['last'] is None
    disk = json.loads(agent.SITES_CONFIG_FILE.read_text(encoding='utf-8'))
    assert disk['notifications']['digest']['interval_minutes'] == 30
    assert http_server('GET', '/notifications')[1]['notifications']['digest'] == config(30)['digest']
