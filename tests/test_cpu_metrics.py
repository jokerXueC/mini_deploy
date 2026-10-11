import pytest

import cpu_metrics


@pytest.fixture
def samples(monkeypatch):
    files = {'/proc/stat': '', '/proc/cpuinfo': ''}
    reads = []

    def read(path, **_kwargs):
        key = path.as_posix()
        reads.append(key)
        if files[key] is None:
            raise OSError('unavailable')
        return files[key]

    monkeypatch.setattr(cpu_metrics.Path, 'read_text', read)
    monkeypatch.setattr(cpu_metrics.os, 'cpu_count', lambda: 8)
    return cpu_metrics.CpuMetrics(), files, reads


def test_aggregate_and_each_logical_cpu_exclude_double_counted_guest_time(samples):
    metrics, files, _ = samples
    files['/proc/stat'] = 'cpu 100 0 20 180 0 0 0 0 50 0\ncpu0 50 0 10 90 0 0 0 0 25 0\ncpu1 50 0 10 90 0 0 0 0 25 0\n'
    assert metrics.sample() is None
    assert all(core['percent'] is None for core in metrics.snapshot['cores'])
    files['/proc/stat'] = 'cpu 160 0 20 220 0 0 0 0 100 0\ncpu0 95 0 10 95 0 0 0 0 65 0\ncpu1 65 0 10 125 0 0 0 0 35 0\n'
    assert metrics.sample() == 60
    assert metrics.snapshot['cores'] == [{'id': 0, 'percent': 90}, {'id': 1, 'percent': 30}]


def test_topology_counts_distinct_socket_core_pairs_and_is_cached(samples, monkeypatch):
    metrics, files, reads = samples
    clock = [10]
    monkeypatch.setattr(cpu_metrics.time, 'monotonic', lambda: clock[0])
    files['/proc/stat'] = 'cpu 10 0 0 90\n' + ''.join(f'cpu{i} 10 0 0 90\n' for i in range(4))
    files['/proc/cpuinfo'] = '\n\n'.join(
        f'processor : {i}\nphysical id : {i // 2}\ncore id : 0\nmodel name : Example CPU' for i in range(4))
    metrics.sample()
    assert metrics.snapshot['physical_cores'] == 2
    assert metrics.snapshot['logical_count'] == 4
    assert metrics.snapshot['sockets'] == 2
    assert metrics.snapshot['model'] == 'Example CPU'
    clock[0] += 1
    metrics.sample()
    assert reads.count('/proc/cpuinfo') == 1
    clock[0] += 60
    metrics.sample()
    assert reads.count('/proc/cpuinfo') == 2


def test_sparse_ids_hotplug_and_counter_reset_do_not_invent_usage(samples):
    metrics, files, _ = samples
    files['/proc/stat'] = 'cpu 10 0 0 90\ncpu2 10 0 0 90\ncpu9 10 0 0 90\n'
    metrics.sample()
    files['/proc/stat'] = 'cpu 20 0 0 180\ncpu2 20 0 0 180\ncpu17 20 0 0 180\n'
    metrics.sample()
    assert metrics.snapshot['cores'] == [{'id': 2, 'percent': 10}, {'id': 17, 'percent': None}]
    files['/proc/stat'] = 'cpu 1 0 0 9\ncpu2 1 0 0 9\n'
    assert metrics.sample() is None
    assert metrics.snapshot['cores'] == [{'id': 2, 'percent': None}]


def test_unavailable_stats_and_arm_topology_remain_unknown(samples):
    metrics, files, _ = samples
    files['/proc/cpuinfo'] = 'processor : 0\nCPU architecture : 8\n\nHardware : ARM test CPU\n'
    files['/proc/stat'] = 'cpu 1 0 0 9\ncpu0 1 0 0 9\n'
    metrics.sample()
    assert metrics.snapshot['model'] == 'ARM test CPU'
    assert metrics.snapshot['physical_cores'] is None
    files['/proc/stat'] = None
    assert metrics.sample() is None
    assert metrics.snapshot['cores'] == []
    assert metrics.snapshot['physical_cores'] is None
    assert metrics.snapshot['logical_count'] == 8
    files['/proc/stat'] = 'cpu 100 0 0 900\ncpu0 100 0 0 900\n'
    assert metrics.sample() is None
