import math

import pytest

import agent
import monitoring
import resource_alerts
from server_performance import History
from test_server_performance import sample
from test_deployment_removal import runtime as runtime


@pytest.fixture
def monitor(tmp_path):
    clock = [10000.]
    monitor = monitoring.Monitor(tmp_path / 'monitor.json', clock=lambda: clock[0])
    return monitor, clock


def feed(instance, clock, cpu=95, memory=50, at=None, scene=None):
    if at is not None:
        clock[0] = at
    server = sample(cpu, memory)
    server['sampled_ts'] = clock[0]
    resource_alerts.evaluate(instance, [server], scene)


def test_one_second_series_catches_dips_and_gap_does_not_count_as_sustained(monitor):
    m, clock = monitor
    for i in range(61):
        feed(m, clock, cpu=50 if i == 30 else 95, at=10000 + i)
    assert not m.snapshot()['alerts']
    feed(m, clock, at=10100)  # A sampling gap resets the pending streak.
    assert not m.snapshot()['alerts']
    for i in range(1, 61):
        feed(m, clock, at=10100 + i)
    assert [a['id'] for a in m.snapshot()['alerts']] == ['server:cpu']
    # Missing data must never count as a healthy sample or recover the incident.
    feed(m, clock, cpu=math.nan, at=10161)
    assert m.snapshot()['alerts'][0]['stale']
    assert m.next_notification(True) is None


def test_hysteresis_stable_recovery_notifications_and_frozen_scene(monitor):
    m, clock = monitor
    scene = {'available': True, 'at': 10060., 'run': 'a' * 32, 'ts': 10050, 'metrics': {},
        'processes': [{'pid': 44, 'name': 'api', 'cpu_percent': 390, 'container': 'api', 'container_id': 'b' * 64}]}
    for i in range(61):
        feed(m, clock, at=10000 + i, scene=scene if i == 60 else None)
    alert = m.snapshot()['alerts'][0]
    assert alert['metadata']['process_snapshot']['processes'][0]['pid'] == 44
    key, message = m.next_notification(True)
    m.delivered(key, message, [{'enabled': True, 'ok': True}])
    for i in range(61, 76):
        feed(m, clock, cpu=84, at=10000 + i)
    assert m.snapshot()['alerts'][0]['metadata']['recovering']
    assert m.next_notification(True) is None
    feed(m, clock, cpu=88, at=10076)  # The hysteresis band cancels recovery.
    for i in range(77, 108):
        feed(m, clock, cpu=70, at=10000 + i)
    assert not m.snapshot()['alerts']
    key, message = m.next_notification(True)
    assert message['recovery']
    events = m.snapshot()['events']
    assert events[0]['kind'] == 'recovery' and events[1]['kind'] == 'alert'
    assert events[0]['metadata']['process_snapshot']['processes'][0]['pid'] == 44


def test_memory_growth_requires_a_continuous_baseline_and_pressure_gate(monitor):
    m, clock = monitor
    for i in range(130):
        clock[0] = 10000 + i
        server = sample(10, 75)
        server['sampled_ts'] = clock[0]
        server['memory']['used_mb'] = 1024 + i * 8
        resource_alerts.evaluate(m, [server])
    assert 'server:memory_growth' in {a['id'] for a in m.snapshot()['alerts']}
    m2 = monitoring.Monitor(m.path.with_name('other.json'), clock=lambda: clock[0])
    for i in range(130):
        clock[0] = 11000 + i
        server['sampled_ts'] = clock[0]
        server['memory'].update(used_mb=1024 + i * 8, percent=30)
        resource_alerts.evaluate(m2, [server])
    assert not m2.snapshot()['alerts']


def test_settings_restart_and_disabled_rules_preserve_incidents(monitor):
    m, clock = monitor
    for i in range(61):
        feed(m, clock, at=10000 + i)
    m.flush()
    restored = monitoring.Monitor(m.path, clock=lambda: clock[0])
    feed(restored, clock, cpu=70, at=10061)
    assert restored.snapshot()['alerts']
    restored.operation({'action': 'settings', 'resource_enabled': False})
    feed(restored, clock, cpu=10, at=10062)
    assert restored.snapshot()['alerts'] and restored.next_notification(True) is None
    assert restored.coverage['server']['status'] == 'disabled'
    for field, value in [('cpu_threshold', True), ('load_threshold_percent', 20), ('memory_growth_mb', -1), ('resource_enabled', 'yes')]:
        with pytest.raises(ValueError):
            restored.operation({'action': 'settings', field: value})


def test_late_duplicate_batches_cannot_reset_streak_and_clock_rollback_does(monitor):
    m, clock = monitor
    for i in range(61):
        feed(m, clock, at=10000 + i)
        stale = sample(95)
        stale['sampled_ts'] = 10000 + i - 1
        resource_alerts.evaluate(m, [stale])
    assert m.snapshot()['alerts']
    fresh = monitoring.Monitor(m.path.with_name('fresh.json'), clock=lambda: clock[0])
    for i in range(30):
        feed(fresh, clock, at=11000 + i)
    feed(fresh, clock, at=10500)
    for i in range(1, 40):
        feed(fresh, clock, at=10500 + i)
    assert not fresh.snapshot()['alerts']


def test_diagnostics_reads_persistent_samples_scenes_and_related_events(runtime, monkeypatch):
    import time
    now = time.time()
    history = History(runtime)
    server = sample(95)
    for i in range(65):
        history.observe(server, now - 65 + i)
    history.record_context({'at': now - 10, 'available': True, 'warnings': [],
        'processes': [{'pid': 55, 'name': 'old-api', 'cpu_percent': 500, 'rss_mib': 900,
                       'service': 'api.service', 'container_id': ''}]}, server)
    history.flush(now, force=True)
    monkeypatch.setattr(agent, '_server_performance', lambda: History(runtime))  # Simulated restart.
    monkeypatch.setattr(agent, '_operations_inventory', lambda *_: {'sources': [{'id': 'service:api.service', 'kind': 'service'}]})
    monkeypatch.setattr(agent, '_realtime_metrics_payload', lambda: {'server': {}, 'realtime_history': []})
    m = agent._monitor()
    m.observe('server:cpu', True, 'CPU high', '95%', 'server', immediate=True)
    m.record_event('lifecycle', 'api restart', '', 'docker_events')
    result = agent._incident_diagnostics('server:cpu')
    assert result['resource_samples'] and result['process_scenes']
    assert result['process_scenes'][0]['processes'][0]['pid'] == 55
    assert result['related_events'][0]['title'] == 'api restart'
    assert result['log_source'] == 'service:api.service'


def test_frozen_container_scene_does_not_use_another_instance():
    scene = {'available': True, 'at': 10000, 'run': 'a' * 32, 'ts': 9990, 'metrics': {},
        'processes': [{'pid': 10, 'name': 'api', 'container_id': 'a' * 64, 'cpu_percent': 90},
                      {'pid': 11, 'name': 'api', 'container_id': 'b' * 64, 'cpu_percent': 300}]}
    frozen = resource_alerts.frozen_scene(scene, 'oom_killed', 'a' * 64)
    assert [p['pid'] for p in frozen['processes']] == [10]
    assert resource_alerts.frozen_scene(scene, 'oom_killed', 'c' * 64) is None


def test_upgrade_never_invents_fault_time_scene_for_existing_incident(runtime, monkeypatch):
    import time
    now = time.time()
    m = agent._monitor()
    m.observe('server:cpu', True, 'CPU high', '95%', 'server', immediate=True, observed_at=now - 3600)
    history = History(runtime)
    server = sample(95)
    server['sampled_ts'] = now
    history.record_context({'at': now, 'available': True, 'warnings': [], 'processes': [{'pid': 19, 'name': 'new'}]}, server)
    monkeypatch.setattr(agent, '_server_performance', lambda: history)
    resource_alerts.evaluate(m, [server], history.latest_scene)
    agent._evaluate_resource_alerts(m, {'server': server, 'docker': {'available': False, 'error': 'docker command not found'}})
    assert not m.snapshot()['alerts'][0]['metadata'].get('process_snapshot')
