import json
from types import SimpleNamespace

import pytest

import agent
import observation_analysis as analysis
from performance_metrics import ContainerHistory
from request_history import History as RequestHistory
from server_performance import History
from test_deployment_removal import runtime as runtime, http_server as http_server
from test_server_performance import sample

NOW = 1_800_000_000


@pytest.fixture
def histories(tmp_path):
    server, docker, requests = History(tmp_path), ContainerHistory(tmp_path), RequestHistory(tmp_path)
    for at, cpu in [(NOW - 60, 5), (NOW + 1, 20), (NOW + 30, 40), (NOW + 31, 40)]:
        server.observe(sample(cpu), at)
    for at, cpu in [(NOW - 60, 5), (NOW, 10), (NOW + 90, 30)]:
        docker.record([{'id':'a' * 64, 'name':'api', 'cpu_percent':cpu, 'memory_percent':40}], at)
    docker.record([{'id':'b' * 64, 'name':'api', 'cpu_percent':80, 'memory_percent':90}], NOW + 90)
    for at, cpu, ticks in [(NOW + 1, 10, 2), (NOW + 90, 30, 3)]:
        server.record_context({'at':at, 'available':True, 'warnings':[], 'processes':[
            {'pid':1, 'start_ticks':1, 'name':'same', 'host_cpu_percent':cpu, 'rss_mib':100 + cpu},
            {'pid':2, 'start_ticks':ticks, 'name':'reused', 'host_cpu_percent':cpu, 'rss_mib':100 + cpu},
        ]}, sample(cpu))
    server.flush(NOW + 120, force=True)
    from datetime import datetime, timezone
    for key, offsets in [('api', [-1, 1, 61, 120]), ('other', [1])]:
        lines = []
        for index, offset in enumerate(offsets):
            at = datetime.fromtimestamp(NOW + offset, timezone.utc).isoformat()
            item = {'at':at, 'host':'example.com', 'method':'GET', 'path':'/users/12',
                    'status':'500' if index == 2 else '200', 'duration':str(index + .1), 'upstream':'0.01'}
            lines.append(at + ' mini_deploy_req ' + json.dumps(item))
        requests.ingest(key, key, 'test', lines, NOW + 120, now=NOW + 120)
    return server, docker, requests


def test_aligned_weighted_ranges_and_identity_safe_growth(histories):
    server, docker, requests = histories
    data = analysis.report(server, docker, requests, SimpleNamespace(snapshot=lambda:{'events':[
        {'at_ts':NOW, 'title':'included'}, {'at_ts':NOW + 120, 'title':'excluded'}]}),
        since=NOW, until=NOW + 120, now=NOW + 120)
    assert data['errors'] == {}
    assert data['resources']['metrics']['cpu_percent']['average'] == pytest.approx(100 / 3)
    assert data['resources']['coverage_seconds'] == 3
    assert data['reference']['metrics']['cpu_percent']['average'] == 5
    assert [e['title'] for e in data['events']] == ['included']
    first, rebuilt = sorted(data['containers'], key=lambda c:c['container'])
    assert first['cpu_growth'] == 20
    assert first['cpu_change']['delta'] == 15
    assert rebuilt['reference'] == {} and rebuilt['cpu_change']['delta'] is None
    processes = {p['name']:p for p in data['processes']['items']}
    assert processes['same']['cpu_growth'] == 20
    assert processes['reused']['cpu_growth'] is None
    assert not processes['reused']['comparable']


def test_request_distribution_source_scoping_and_half_open_boundary(histories):
    identity = ['api','example.com','GET','/users/:number']
    result = analysis.requests(histories[2], NOW, NOW + 120, 'api', [identity])
    assert result['count'] == 2 and result['server_errors'] == 1
    assert sum(b['count'] for b in result['distribution']) == result['duration_count'] == 2
    assert result['p99'] >= 2100 and result['p95'] >= 2100
    assert result['watched'][0]['result']['count'] == 2
    assert result['slow_endpoints'][0]['identity'] == identity
    assert analysis.requests(histories[2], NOW, NOW + 120)['count'] == 3
    assert analysis.requests(histories[2], NOW + 180, NOW + 240)['p95'] is None
    assert not analysis.requests(histories[2], NOW, NOW + 120, 'missing')['available']


def test_missing_baselines_zero_denominators_and_source_failure(histories, monkeypatch):
    assert analysis.change(10, 0) == {'delta':10, 'percent':None}
    assert analysis.change(10, None) == {'delta':None, 'percent':None}
    server, docker, requests = histories
    monkeypatch.setattr(docker, 'connect', lambda: (_ for _ in ()).throw(OSError('unavailable')))
    result = analysis.report(server, docker, requests, SimpleNamespace(snapshot=lambda:{'events':[]}),
        since=NOW, until=NOW + 120, compare='yesterday', now=NOW + 120)
    assert result['errors'].keys() == {'containers'}
    assert result['containers'] is None and result['resources']['statistics']
    assert result['reference']['metrics']['cpu_percent']['average'] is None
    assert result['reference']['coverage_percent'] == 0


@pytest.mark.parametrize('since,until', [(0,float('nan')), (0,float('inf')), (100,1), (-1,100), (0,604801)])
def test_invalid_windows_rejected(since, until):
    with pytest.raises(ValueError):
        analysis.window(since,until,now=NOW)


def test_alignment_custom_baseline_and_invalid_watches(histories):
    assert analysis.window(NOW + 1, NOW + 61, NOW + 120) == (NOW,NOW + 120)
    assert analysis.window(NOW - 604800 + 1, NOW + 1, NOW + 120)[1] - analysis.window(NOW - 604800 + 1, NOW + 1, NOW + 120)[0] == 604800
    result = analysis.report(*histories, SimpleNamespace(snapshot=lambda:{'events':[]}),
        since=NOW, until=NOW + 120, compare='custom', reference=NOW - 60, now=NOW + 120)
    assert (result['reference_since'],result['reference_until']) == (NOW - 180, NOW - 60)
    for watches in ({}, ['bad'], [[1,2,3,4]], [['a'] * 4] * 9):
        with pytest.raises(ValueError):
            analysis.requests(histories[2], NOW, NOW + 120, watches=watches)


def test_routes_require_login_and_validate_filters(http_server, histories, monkeypatch):
    original_window = analysis.window
    monkeypatch.setattr(analysis, 'window', lambda since,until,now=None:original_window(since,until,NOW+120))
    monkeypatch.setattr(agent, '_request_history', lambda:histories[2])
    for endpoint in ('analyze','requests'):
        path = f'/observations/{endpoint}?since={NOW}&until={NOW+120}'
        assert http_server('GET',path,authenticated=False)[0] == 401
        assert http_server('GET',f'/observations/{endpoint}?since=nan&until=inf')[0] == 400
    code, payload = http_server('GET',f'/observations/requests?since={NOW}&until={NOW+120}&source=api')
    assert code == 200 and payload['count'] == 2
    assert http_server('GET',f'/observations/requests?since={NOW}&until={NOW+120}&watch=%7B%7D')[0] == 400
