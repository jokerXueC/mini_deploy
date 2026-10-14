import http.client
import json
import time

import pytest

import agent
from prometheus_metrics import Access, render
from test_server_performance import sample
from test_deployment_removal import runtime as runtime, http_server as http_server


def raw(server, method='GET', path='/metrics', token='', body=None, cookie=False, csrf=False):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    if cookie:
        headers['Cookie'] = f'{agent.COOKIE_NAME}={server.cookie}'
    if csrf:
        headers['X-CSRF-Token'] = agent._csrf_token(server.cookie)
    connection = http.client.HTTPConnection(server.origin.removeprefix('http://'))
    connection.request(method, path, json.dumps(body) if body is not None else None, headers)
    response = connection.getresponse()
    data = response.read()
    result = response.status, dict(response.getheaders()), data
    connection.close()
    return result


def test_read_only_bearer_rotation_csrf_and_cache_only_scraping(http_server, runtime, monkeypatch):
    assert raw(http_server)[0] == 401
    assert raw(http_server, 'POST', '/metrics/access', body={'action': 'enable'}, cookie=True)[0] == 403
    code, payload = http_server('POST', '/metrics/access', {'action': 'enable'})
    assert code == 200 and payload['enabled']
    token = payload['token']
    now = time.time()
    server = sample()
    server['sampled_ts'] = now
    monkeypatch.setattr(agent, '_realtime_metrics_payload', lambda: {'server': server})
    monkeypatch.setattr(agent, '_system_status_payload', lambda: pytest.fail('scrape must not collect Docker'))
    monkeypatch.setattr(agent, '_docker_status', lambda: pytest.fail('scrape must not invoke Docker'))
    code, headers, body = raw(http_server, token=token)
    assert code == 200 and 'version=0.0.4' in headers['Content-Type']
    assert b'mini_deploy_cpu_usage_ratio 0.2' in body and headers['Cache-Control'] == 'no-store'
    assert raw(http_server, path='/status', token=token)[0] == 401
    assert raw(http_server, 'POST', '/monitoring', token=token, body={'action': 'mute', 'seconds': 0})[0] == 401
    assert 'token' not in http_server('GET', '/metrics/access')[1]
    assert token not in (runtime / 'prometheus-access.json').read_text()
    code, changed = http_server('POST', '/metrics/access', {'action': 'rotate'})
    assert code == 200 and raw(http_server, token=token)[0] == 401
    assert raw(http_server, token=changed['token'])[0] == 200
    assert http_server('POST', '/metrics/access', {'action': 'disable'})[0] == 200
    assert raw(http_server, token=changed['token'])[0] == 401


def test_freshness_units_missing_values_and_safe_bounded_labels():
    now = time.time()
    server = sample()
    server['sampled_ts'] = now
    docker = {'available': True, 'containers': [{'name': 'api"\\\nlabel', 'id': 'secret-id', 'state': 'running', 'cpu_percent': 250, 'memory_percent': 12, 'health': 'unhealthy'}]}
    data = render(server, docker, None, now=now, docker_at=now).decode()
    assert 'mini_deploy_container_cpu_cores{container="api\\"\\\\\\nlabel"} 2.5' in data
    assert 'secret-id' not in data and 'pid=' not in data
    assert 'mini_deploy_disk_await_seconds 0.005' in data
    assert 'mini_deploy_network_receive_bytes_per_second 122880' in data
    text_string_to_metric_families = pytest.importorskip('prometheus_client.parser').text_string_to_metric_families
    families = list(text_string_to_metric_families(data))
    assert families and any(f.name == 'mini_deploy_container_cpu_cores' for f in families)
    stale = render(server, docker, None, now=now + 200, docker_at=now).decode()
    assert 'mini_deploy_server_sample_available 0' in stale and 'cpu_usage_ratio' not in stale
    assert 'container_cpu_cores' not in stale
    server['memory']['percent'] = float('nan')
    missing = render(server, {}, None, now=now).decode()
    assert 'memory_usage_ratio' not in missing and 'NaN' not in missing and 'Inf' not in missing


def test_corrupt_and_hardlinked_credentials_are_preserved(tmp_path):
    access = Access(tmp_path)
    access.path.write_text('{corrupt')
    with pytest.raises(ValueError):
        access.change('enable')
    assert access.path.read_text() == '{corrupt'
    other = tmp_path / 'other'
    other.mkdir()
    shared = other / 'business'
    shared.write_text('{}')
    try:
        (other / 'prometheus-access.json').hardlink_to(shared)
    except OSError:
        pytest.skip('hardlinks unavailable')
    with pytest.raises(ValueError):
        Access(other).change('enable')
    assert shared.read_text() == '{}'
