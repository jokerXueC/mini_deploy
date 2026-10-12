import json
import sqlite3

import pytest

from performance_metrics import ContainerHistory, DiskSampler
from request_history import History
from test_request_history import NOW, ingest, line


def test_disk_deltas_handle_first_reset_missing_and_multiple_devices(monkeypatch):
    sampler = DiskSampler()
    data = [{'vda': (100, 1000, 20, 100, 2000, 20, 50)},
            {'vda': (104, 1040, 30, 106, 2080, 40, 150)},
            {'vda': (0, 0, 0, 0, 0, 0, 0)}]
    cpu = [(100, 10), (200, 20), (300, 20)]
    monkeypatch.setattr(sampler, 'counters', lambda: (data.pop(0), cpu.pop(0)))
    assert sampler.sample(1)['devices'] == []
    result = sampler.sample(3)
    device = result['devices'][0]
    assert device['read_kib_s'] == 10 and device['write_kib_s'] == 20
    assert device['iops'] == 5 and device['await_ms'] == 3
    assert device['busy_percent'] == 5 and result['iowait_percent'] == 10
    assert sampler.sample(4)['devices'] == []
    monkeypatch.setattr(sampler, 'counters', lambda: (_ for _ in ()).throw(OSError('missing')))
    assert sampler.sample(5) == {'available': False, 'devices': [], 'iowait_percent': None}
    assert sampler.previous is None


def test_disk_counters_exclude_partitions_virtual_duplicates_and_guest(tmp_path):
    proc, sys = tmp_path / 'proc', tmp_path / 'sys'
    proc.mkdir()
    sys.mkdir()
    for name in ('vda', 'dm-0', 'loop0'):
        (sys / name / 'slaves').mkdir(parents=True)
    (sys / 'dm-0/slaves/vda').touch()
    (proc / 'diskstats').write_text('\n'.join(f'1 0 {name} 1 2 3 4 5 6 7 8 9 10 11' for name in ('vda', 'vda1', 'dm-0', 'loop0')))
    (proc / 'stat').write_text('cpu 10 20 30 40 50 60 70 80 999 999\n')
    disks, cpu = DiskSampler(proc, sys).counters()
    assert set(disks) == {'vda'} and cpu == (360, 50)


def test_container_history_persists_deduplicates_retains_cpu_above100_and_gaps(tmp_path):
    store = ContainerHistory(tmp_path, max_rows=3)
    item = {'id': 'a' * 64, 'name': 'api', 'cpu_percent': 250, 'memory_percent': 20}
    store.record([item], now=NOW - 100)
    store.record([{**item, 'cpu_percent': 10}], now=NOW - 99)
    store.record([item], now=NOW - 30)
    data = ContainerHistory(tmp_path).query(item['id'], '1h', NOW)
    assert len(data['points']) == 2 and data['points'][0]['cpu_percent'] == 250
    assert data['points'][1]['ts'] - data['points'][0]['ts'] > 30
    assert store.query('b' * 64, now=NOW)['points'] == []
    store.record([item], now=NOW + 8 * 86400)
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 1
    for second in range(4):
        store.record([item], now=NOW + 8 * 86400 + (second + 1) * 30)
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 3
    with pytest.raises(ValueError):
        store.query('../data')


def test_percentiles_use_entire_group_not_recent30_or_matching_details(tmp_path):
    store = History(tmp_path)
    lines = [line('/api/items/123', ts=NOW - 500 + index, duration=str((index + 1) / 1000)) for index in range(100)]
    ingest(store, lines)
    result = store.query('api', now=NOW, search='123')
    group = result['groups'][0]
    assert group['p95'] == 95 and group['p99'] == 99
    assert len(group['items']) == 30 and group['durationCount'] == 100
    assert store.query('api', status='slow', slow_ms=99, now=NOW)['groups'][0]['p95'] == 95
    assert store.query('api', layout='flat', now=NOW)['groups'][0]['p99'] == 99
    assert store.query('api', now=NOW, period='1h')['groups'][0]['p95'] == 95
    assert json.loads(json.dumps(group))['p99'] == 99
