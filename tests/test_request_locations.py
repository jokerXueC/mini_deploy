import json
import threading

import pytest

import agent
from request_locations import Locations
from request_history import History
from test_request_history import NOW, line
from test_deployment_removal import http_server as http_server, runtime as runtime


def record(ip='8.8.8.8', **kwargs):
    stamp, payload = line(**kwargs).split(' mini_deploy_req ', 1)
    return stamp + ' mini_deploy_req ' + json.dumps({**json.loads(payload), 'client_ip': ip})


@pytest.fixture
def locations(tmp_path, monkeypatch):
    history = History(tmp_path)
    instance = Locations(history, clock=lambda: NOW)
    monkeypatch.setattr(instance, '_schedule', lambda *args: None)
    return instance


def test_locations_count_entire_retained_period_and_normalize_ips(locations):
    history = locations.history
    lines = [record(path=f'/busy/{i}') for i in range(1001)]
    lines += [record('::ffff:8.8.8.8', status=503), record('10.0.0.2'), record('bad-ip'),
              record(ts=NOW - 4000), record(ts=NOW + 1)]
    history.ingest('api', 'API', 'container', lines, NOW, now=NOW)
    history.ingest('other', 'Other', 'container', [record('9.9.9.9')], NOW, now=NOW)
    result = locations.history_summary('api', period='1h')
    assert result['total_requests'] == 1004 and result['missing_ip_requests'] == 1
    assert result['total_ips'] == 2 and not result['limited']
    public, private = result['items']
    assert public['ip'] == '8.8.8.8' and public['count'] == 1002 and public['errors'] == 1
    assert public['successes'] == 1001 and public['last_at'] == NOW - 10
    assert private['location'] == {'label': '内网 / 保留地址'}
    assert locations.history_summary('api', period='1h', status='error')['total_requests'] == 1
    assert locations.history_summary('api', period='1h', search='/busy/1000')['total_requests'] == 1
    assert locations.history_summary('api', period='1h', status='slow', slow_ms=100)['total_requests'] == 0
    with pytest.raises(ValueError):
        locations.history_summary('api', slow_ms=True)


def test_locations_do_not_reconstruct_ips_from_retained_rollups(tmp_path, monkeypatch):
    history = History(tmp_path, max_rows=1)
    history.ingest('api', 'API', 'c', [record('9.9.9.9', ts=NOW - 100), record()], NOW, now=NOW)
    locations = Locations(history, clock=lambda: NOW)
    monkeypatch.setattr(locations, '_schedule', lambda *args: None)
    assert history.query('api', now=NOW)['total_requests'] == 2
    result = locations.history_summary('api')
    assert result['total_requests'] == 1 and result['total_ips'] == 1
    assert result['items'][0]['ip'] == '8.8.8.8'


def test_locations_limit_keeps_exact_totals_and_missing_count(locations):
    lines = [record(f'11.1.{i // 250}.{i % 250 + 1}') for i in range(510)] + [record('')]
    locations.history.ingest('api', 'API', 'c', lines, NOW, now=NOW)
    result = locations.history_summary('api')
    assert result['total_requests'] == 511 and result['total_ips'] == 510
    assert result['limited'] and len(result['items']) == 500 and result['missing_ip_requests'] == 1


def test_geolocation_runs_in_background_is_bounded_and_survives_restart(tmp_path):
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def locator(ip):
        calls.append(ip)
        entered.set()
        release.wait(3)
        if len(calls) == 2:
            done.set()
        return {'label': '测试地区', 'latitude': 31.2, 'longitude': 121.4}
    history = History(tmp_path)
    instance = Locations(history, clock=lambda: NOW, locator=locator)
    records = [{'client_ip': ip, 'status': 200} for ip in ('8.8.8.8', '9.9.9.9', '1.1.1.1', '10.0.0.2')]
    try:
        result = instance.sample_summary(records)
        assert len(result['items']) == 4 and entered.wait(1)
        assert result['items'][0]['location']['label'] in {'待定位', '内网 / 保留地址'}
        instance.sample_summary(records)
        assert len(calls) == 1
    finally:
        release.set()
    assert done.wait(2)
    # Acquiring the worker's lock isn't sufficient to wait for its final DB commit.
    for thread in threading.enumerate():
        if thread.name == 'request-ip-locations':
            thread.join(2)
    assert len(calls) == 2 and all(ip != '10.0.0.2' for ip in calls)
    restored = Locations(History(tmp_path), clock=lambda: NOW, locator=lambda ip: pytest.fail('Cached IP must not be resolved again'))
    cached = restored.sample_summary([{'client_ip': ip, 'status': 200} for ip in calls])
    assert all(row['location']['latitude'] == 31.2 for row in cached['items'])


def test_request_map_auth_full_history_and_location_failure_keeps_records(http_server, runtime, monkeypatch):
    history = agent._request_history()
    now = agent.time.time()
    history.ingest('api', 'API', 'c', [record(ts=now - 10)] * 90, now, now=now)
    instance = Locations(history)
    monkeypatch.setattr(instance, '_schedule', lambda *args: None)
    monkeypatch.setattr(agent, '_request_locations_instance', instance)
    assert http_server('GET', '/request-history?key=api', authenticated=False)[0] == 401
    code, body = http_server('GET', '/request-history?key=api')
    assert code == 200 and body['visitors']['total_requests'] == 90
    assert len(body['groups'][0]['items']) == 30
    monkeypatch.setattr(instance, 'history_summary', lambda **kwargs: (_ for _ in ()).throw(OSError('unavailable')))
    code, body = http_server('GET', '/request-history?key=api')
    assert code == 200 and body['visitors']['error'] and body['groups'][0]['count'] == 90
