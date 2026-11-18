import copy
import json
import sqlite3

import pytest

import agent
import container_monitoring as cm
import container_resources as cr
from monitoring import Monitor
from performance_metrics import ContainerHistory
from test_container_monitoring import inspected
from test_deployment_removal import runtime, http_server
from test_panel_runtime import runtime_evidence, execute

ID = 'a' * 64
NOW = 1800000000


def file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text), encoding='ascii')


@pytest.fixture
def linux(tmp_path):
    proc, cg = tmp_path / 'proc', tmp_path / 'cgroup'
    group = '/system.slice/docker-' + ID + '.scope'
    path = cg / group.lstrip('/')
    file(proc / '321/cgroup', '0::' + group)
    file(path / 'cpu.stat', 'nr_periods 100\nnr_throttled 20\n')
    file(path / 'cpu.max', '50000 100000')
    file(path / 'memory.current', '800')
    file(path / 'memory.max', '1000')
    file(path / 'memory.stat', 'anon 500\nfile 300\ninactive_file 100')
    file(path / 'memory.swap.current', '200')
    file(path / 'memory.events', 'oom 2\noom_kill 1')
    file(path / 'pids.current', '2')
    file(path / 'pids.max', '100')
    file(path / 'memory.pressure', 'some avg10=2.40 avg60=0.0 total=100')
    file(proc / '321/fd/0', '')
    file(proc / '321/fd/1', '')
    file(proc / '321/limits', 'Max open files            10      20      files')
    item = inspected()
    item['State']['Pid'] = 321
    item['HostConfig'].update(Memory=1000, NanoCpus=500000000, PidsLimit=100)
    snapshot = cm.attach_inspection({'available': True, 'containers': [{
        'id': ID, 'name': 'api', 'cpu_percent': 45, 'memory_percent': 70,
        'memory_usage': '700B / 1kB', 'net_io': '100B / 200B', 'block_io': '0B / 0B', 'pids': 2,
    }]}, [item])
    return cr.Sampler(proc, cg), snapshot, proc, path


def test_v2_limits_actual_memory_headroom_rates_and_throttle(linux):
    sampler, snapshot, proc, path = linux
    resource = sampler.sample(copy.deepcopy(snapshot), 100)['containers'][0]['resources']
    assert resource['cpu_limit_cores'] == .5 and resource['cpu_limit_percent'] == 90
    assert resource['memory_used_bytes'] == 700 and resource['memory_current_bytes'] == 800
    assert resource['memory_headroom_bytes'] == 200 and resource['memory_limit_percent'] == 80
    assert resource['memory_cache_bytes'] == 300 and resource['memory_swap_bytes'] == 200
    assert resource['main_process_fd_percent'] == 20 and resource['pids_percent'] == 2
    assert resource['memory_pressure_percent'] == 2.4
    assert resource['network_rx_bytes_s'] is None and resource['cpu_throttled_percent'] is None
    file(path / 'cpu.stat', 'nr_periods 130\nnr_throttled 35\n')
    snapshot['containers'][0]['net_io'] = '400B / 800B'
    snapshot['containers'][0]['block_io'] = '300B / 600B'
    resource = sampler.sample(copy.deepcopy(snapshot), 130)['containers'][0]['resources']
    assert resource['network_rx_bytes_s'] == 10 and resource['network_tx_bytes_s'] == 20
    assert resource['block_read_bytes_s'] == 10 and resource['block_write_bytes_s'] == 20
    assert resource['cpu_throttled_percent'] == 50 and resource['oom_kill_total'] == 1


def test_resets_restarts_gaps_and_missing_sources_are_unknown(linux):
    sampler, snapshot, proc, path = linux
    sampler.sample(copy.deepcopy(snapshot), 100)
    snapshot['containers'][0]['net_io'] = '0B / 0B'
    assert sampler.sample(copy.deepcopy(snapshot), 130)['containers'][0]['resources']['network_rx_bytes_s'] is None
    snapshot['containers'][0]['inspection']['started_at'] = 'new-start'
    assert sampler.sample(copy.deepcopy(snapshot), 160)['containers'][0]['resources']['cpu_throttled_percent'] is None
    assert sampler.sample(copy.deepcopy(snapshot), 300)['containers'][0]['resources']['network_rx_bytes_s'] is None
    snapshot['containers'][0]['memory_usage'] = ''
    snapshot['containers'][0]['net_io'] = ''
    resource = sampler.sample(copy.deepcopy(snapshot), 330, local=False)['containers'][0]['resources']
    assert resource['network_rx_bytes_s'] is None and resource['memory_headroom_bytes'] is None
    assert resource['cgroup_version'] == 'unavailable'


def test_v1_cgroups_and_unlimited_configuration(tmp_path):
    proc, cg = tmp_path / 'proc', tmp_path / 'cg'
    group = '/docker/' + ID
    file(proc / '1/cgroup', f'2:cpu,cpuacct:{group}\n3:memory:{group}\n4:pids:{group}')
    file(cg / 'cpu' / group.lstrip('/') / 'cpu.stat', 'nr_periods 10\nnr_throttled 2')
    file(cg / 'cpu' / group.lstrip('/') / 'cpu.cfs_quota_us', '-1')
    file(cg / 'cpu' / group.lstrip('/') / 'cpu.cfs_period_us', '100000')
    memory = cg / 'memory' / group.lstrip('/')
    file(memory / 'memory.usage_in_bytes', '800')
    file(memory / 'memory.limit_in_bytes', str(2**63 - 4096))
    file(memory / 'memory.stat', 'total_rss 500\ntotal_cache 300\ntotal_inactive_file 100')
    file(memory / 'memory.memsw.usage_in_bytes', '1000')
    file(cg / 'pids' / group.lstrip('/') / 'pids.max', 'max')
    result, counters = cr.Sampler(proc, cg).linux({'pid': 1}, ID)
    assert result['cgroup_version'] == 'v1'
    assert result.get('memory_limit_bytes') is None and result['memory_swap_bytes'] == 200
    assert result['pids_limit'] is None and counters.get('oom_kills') is None


def test_identity_scope_budget_units_and_safe_inspection(linux):
    sampler, snapshot, proc, path = linux
    assert cr.byte_value('1.5MiB') == 1572864 and cr.byte_value('1.5MB') == 1500000
    assert cr.byte_value('nan MB') is None
    item = inspected()
    item['HostConfig'].update(CpusetCpus='0-2,4', CpuQuota=50000, CpuPeriod=100000,
                              LogConfig={'Type': 'json-file', 'Config': {'secret': 'hidden'}})
    item['Config']['Env'] = ['TOKEN=hidden']
    evidence = cr.inspection(item)
    assert evidence['cpuset_cores'] == 4 and 'hidden' not in json.dumps(evidence)
    file(proc / '321/cgroup', '0::/another-container')
    resource = sampler.sample(snapshot, 100)['containers'][0]['resources']
    assert resource['cgroup_version'] == 'unavailable' and resource.get('memory_anon_bytes') is None
    assert sampler.location('v2', '/../' + ID) is None


def test_log_growth_and_rotation_do_not_become_negative(linux, tmp_path):
    sampler, snapshot, proc, path = linux
    log = tmp_path / 'current.log'
    file(log, 'a' * 100)
    config = snapshot['containers'][0]['inspection']['resource_config']
    config.update(log_driver='json-file', log_path=str(log))
    sampler.sample(copy.deepcopy(snapshot), 100)
    file(log, 'a' * 400)
    assert sampler.sample(copy.deepcopy(snapshot), 130)['containers'][0]['resources']['log_bytes_s'] == 10
    file(log, 'a' * 50)
    assert sampler.sample(copy.deepcopy(snapshot), 160)['containers'][0]['resources']['log_bytes_s'] is None


def test_history_compaction_keeps_weighted_average_spikes_and_other_observers(tmp_path):
    history = ContainerHistory(tmp_path)
    for at, cpu in [(NOW - 300, 10), (NOW - 270, 90), (NOW - 240, 20)]:
        history.record([{'id': ID, 'name': 'api', 'cpu_percent': cpu, 'memory_percent': 20,
            'resources': {'network_rx_bytes_s': cpu * 10, 'cpu_throttled_percent': cpu / 2}}], at)
    history.record([], NOW + 7 * 3600)
    with history.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM resource_samples').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM container_rollups').fetchone()[0] == 1
    result = history.query(ID, '24h', NOW + 7 * 3600)
    assert result['summary']['cpu_percent']['average'] == 40
    assert result['summary']['cpu_percent']['peak'] == 90
    assert result['summary']['network_rx_bytes_s']['average'] == 400
    aggregate = history.aggregate(NOW - 600, NOW, ID)[0]
    assert aggregate['cpu'] == 40 and aggregate['samples'] == 3
    assert result['coverage_percent'] < 1 and result['latest_at'] == NOW - 240
    # A later record in a partially compacted bucket must be counted exactly once.
    history.record([{'id': ID, 'name': 'api', 'cpu_percent': 40}], NOW - 210)
    history.record([], NOW + 7 * 3600)
    assert history.aggregate(NOW - 600, NOW, ID)[0]['samples'] == 4


def test_history_upgrades_old_database_without_rewriting_raw_rows(tmp_path):
    history = ContainerHistory(tmp_path)
    history.path.parent.mkdir(parents=True)
    with sqlite3.connect(history.path) as db:
        db.execute('CREATE TABLE samples(container TEXT,ts INTEGER,name TEXT,cpu REAL,memory REAL,PRIMARY KEY(container,ts))')
        db.execute('INSERT INTO samples VALUES(?,?,?,?,?)', (ID, NOW, 'legacy', 80, 20))
    result = history.query(ID, '1h', NOW + 60)
    assert result['points'][0]['cpu_percent'] == 80
    assert result['points'][0]['network_rx_bytes_s'] is None
    with history.connect() as db:
        assert db.execute('SELECT cpu FROM samples').fetchone()[0] == 80


def test_configured_memory_limit_alone_is_not_runtime_coverage(tmp_path):
    history = ContainerHistory(tmp_path)
    history.record([{'id': ID, 'name': 'stopped', 'resources': {'memory_limit_bytes': 1024}}], NOW)
    assert history.query(ID, '1h', NOW + 30)['coverage_percent'] == 0


def test_alert_sustain_hysteresis_missing_samples(tmp_path):
    clock = [1000.0]
    monitor = Monitor(tmp_path / 'monitor.json', clock=lambda: clock[0])
    item = cm.attach_inspection({'available': True, 'containers': [{'id': ID, 'name': 'api'}]}, [inspected()])
    for at in [1000, 1030, 1061]:
        clock[0] = at
        item['containers'][0]['resources'] = {'sampled_at': at, 'memory_limit_percent': 95}
        cr.evaluate(monitor, item)
    assert len(monitor.snapshot()['alerts']) == 1
    item['containers'][0]['resources']['memory_limit_percent'] = None
    cr.evaluate(monitor, item)
    assert monitor.snapshot()['alerts'][0]['stale']
    item['containers'][0]['resources']['memory_limit_percent'] = 87
    cr.evaluate(monitor, item)
    assert len(monitor.snapshot()['alerts']) == 1
    item['containers'][0]['resources']['memory_limit_percent'] = 80
    cr.evaluate(monitor, item)
    assert not monitor.snapshot()['alerts']


def test_duplicate_compose_identity_and_cached_sample_cannot_trigger_alert(tmp_path):
    clock = [1000.0]
    monitor = Monitor(tmp_path / 'monitor.json', clock=lambda: clock[0])
    evidence = cm.parse_inspect([inspected(project='demo', service='api', replica='1')])[ID]
    row = {'id': ID, 'name': 'api', 'inspection': evidence,
           'resources': {'sampled_at': 1000, 'memory_limit_percent': 95}}
    snapshot = {'available': True, 'containers': [row]}
    for at in [1000, 1030, 1061]:
        clock[0] = at
        cr.evaluate(monitor, snapshot)
    assert not monitor.snapshot()['alerts']
    # The other ID claims the same Compose service/replica, so attribution is unknown.
    other = copy.deepcopy(row)
    other['id'] = other['inspection']['id'] = 'b' * 64
    snapshot['containers'].append(other)
    for at in [1070, 1100, 1140]:
        clock[0] = at
        for item in snapshot['containers']:
            item['resources']['sampled_at'] = at
        cr.evaluate(monitor, snapshot)
    assert not monitor.snapshot()['alerts']
    assert not monitor.incidents


def test_health_duration_and_streak_exclude_health_output():
    item = inspected(health='unhealthy')
    item['State']['Health'].update(FailingStreak=3, Log=[{
        'Start': '2026-11-18T10:00:00.123456789Z', 'End': '2026-11-18T10:00:00.127656789Z',
        'ExitCode': 1, 'Output': 'credential-must-not-be-kept'}])
    evidence = cm.parse_inspect([item])[ID]
    assert evidence['health_last_check']['duration_ms'] == pytest.approx(4.2)
    assert evidence['health_failing_streak'] == 3
    assert 'credential-must-not-be-kept' not in json.dumps(evidence)


def test_capacity_clipping_is_explicit_and_keeps_new_samples(tmp_path):
    history = ContainerHistory(tmp_path, max_rows=2)
    # Preexisting rollups can be trimmed without deleting or rebuilding the database.
    with history.connect() as db:
        db.executemany('INSERT INTO container_rollups VALUES(?,?,?,?,?,?,?)', [
            (ID, NOW - 900 + n * 300, 'api', 1, NOW - 900 + n * 300, NOW - 900 + n * 300,
             json.dumps({'cpu_percent': [20, 1, 20]})) for n in range(3)])
    history.record([{'id': ID, 'name': 'api', 'cpu_percent': 42}], NOW)
    result = history.query(ID, '1h', NOW + 30)
    assert result['capacity_limited'] and result['latest_at'] == NOW
    assert result['summary']['cpu_percent']['peak'] == 42
    with history.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM container_rollups').fetchone()[0] == 2


def test_cached_api_and_ai_share_evidence_without_docker_commands(http_server, runtime, monkeypatch, runtime_evidence):
    history = ContainerHistory(runtime)
    history.record([{'id': ID, 'cpu_percent': 45, 'resources': {'cpu_throttled_percent': 25}}])
    monkeypatch.setattr(agent, '_performance_history', lambda: history)
    monkeypatch.setattr(agent, '_system_status_payload', lambda: {'docker': {'stale': False,
        'containers': [{'id': ID, 'name': 'api', 'resources': {'cpu_limit_cores': .5}}]}})
    monkeypatch.setattr(agent, '_run_command', lambda *a, **kw: pytest.fail('GET must not run commands'))
    assert http_server('GET', '/performance/containers?id=' + ID, authenticated=False)[0] == 401
    code, result = http_server('GET', '/performance/containers?id=' + ID)
    assert code == 200 and result['current']['resources']['cpu_limit_cores'] == .5
    assert result['summary']['cpu_throttled_percent']['peak'] == 25
    runtime_evidence.runtime.containers = agent._container_performance_payload
    data = execute(runtime_evidence.runtime, 'get_container_resources', {'container': ID})
    assert data['current']['id'] == ID and data['point_count'] == 1
    assert not runtime_evidence.commands
    with pytest.raises(Exception, match='完整容器 ID'):
        runtime_evidence.runtime.plan('get_container_resources', {'container': '--all'})


def test_ai_sampling_preserves_spike_bucket_outside_uniform_trend(runtime_evidence):
    payload = {'points': [{'ts': NOW + i * 30, 'cpu_percent': 10,
                           'cpu_percent_peak': 95 if i == 1 else 10} for i in range(121)],
               'interval_seconds': 30, 'summary': {'cpu_percent': {'peak': 95}}}
    runtime_evidence.runtime.containers = lambda *_: copy.deepcopy(payload)
    result = execute(runtime_evidence.runtime, 'get_container_resources', {'container': ID})
    assert len(result['points']) == 60 and result['point_count'] == 121
    assert all(p['ts'] != NOW + 30 for p in result['points'])
    assert result['peak_samples']['cpu_percent'] == {'bucket_at': NOW + 30, 'value': 95, 'bucket_seconds': 30}
