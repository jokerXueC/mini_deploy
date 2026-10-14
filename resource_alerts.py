"""Resource rules evaluated from existing samples, with continuous streaks."""
from collections import deque
import math

from server_performance import values, capacity

DEFAULTS = {'resource_enabled': True, 'cpu_threshold': 90, 'memory_threshold': 90,
    'disk_threshold': 90, 'inode_threshold': 90, 'swap_threshold': 90,
    'iowait_threshold': 20, 'load_threshold_percent': 150,
    'memory_growth_mb': 256, 'resource_recovery_seconds': 30}
RANGES = {**{key: (50, 99) for key in ('cpu_threshold', 'memory_threshold', 'disk_threshold', 'inode_threshold', 'swap_threshold')},
    'iowait_threshold': (5, 90), 'load_threshold_percent': (100, 1000),
    'memory_growth_mb': (16, 32768), 'resource_recovery_seconds': (5, 600)}
RULES = [('cpu', 'CPU 使用率', 'cpu_percent', 'cpu_threshold', '%'),
    ('memory', '内存使用率', 'memory_percent', 'memory_threshold', '%'),
    ('disk', '根分区使用率', 'disk_percent', 'disk_threshold', '%'),
    ('inode', '根分区 inode 使用率', 'inode_percent', 'inode_threshold', '%'),
    ('swap', 'Swap 使用率', 'swap_percent', 'swap_threshold', '%'),
    ('iowait', 'CPU I/O 等待', 'iowait_percent', 'iowait_threshold', '%'),
    ('load', '每核系统负载', 'load_per_core_percent', 'load_threshold_percent', '%'),
    ('memory_growth', '内存持续增长', 'memory_growth_mb', 'memory_growth_mb', ' MiB/分钟')]


def valid(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def frozen_scene(scene, reason, container_id=None):
    if not scene or not scene.get('available'):
        return None
    rows = scene.get('processes', [])
    if container_id:
        rows = [p for p in rows if p.get('container_id') == container_id]
        if not rows:
            return None
    rank = 'rss_mib' if reason in {'memory', 'memory_growth', 'swap', 'oom', 'oom_killed'} else 'write_kib_s' if reason in {'disk', 'inode', 'iowait'} else 'cpu_percent'
    rows = sorted(rows, key=lambda p: p.get(rank) or 0, reverse=True)[:3]
    fields = ('pid', 'start_ticks', 'name', 'cpu_percent', 'rss_mib', 'read_kib_s', 'write_kib_s', 'service', 'container', 'container_id')
    return {'at': scene['at'], 'run': scene['run'], 'ts': scene['ts'], 'metrics': scene.get('metrics', {}),
        'processes': [{k: p.get(k) for k in fields} for p in rows],
        'warnings': [str(w)[:160] for w in scene.get('warnings', [])[:2]]}


def evaluate(monitor, samples, scene=None):
    with monitor.lock:
        if not hasattr(monitor, 'resource_state'):
            monitor.resource_state = {}
            monitor.resource_memory = deque(maxlen=90)
            monitor.resource_last = None
        settings = {**DEFAULTS, **monitor.settings}
        if not settings['resource_enabled']:
            for key in list(monitor.incidents):
                if key.startswith('server:'):
                    monitor.observe(key, None, '', '', 'server')
            monitor.resource_state.clear()
            monitor.coverage['server'] = {'status': 'disabled', 'checked_at': monitor.clock(), 'detail': '资源告警已关闭，历史采集继续。'}
            return
        for sample in samples:
            at = sample.get('sampled_ts', monitor.clock())
            fresh = valid(at) and 0 <= monitor.clock() - at <= 120
            if not fresh:
                for reason, title, _, _, _ in RULES:
                    monitor.observe('server:' + reason, None, title, '采样缺失或过期', 'server')
                monitor.resource_state.clear()
                continue
            previous = monitor.resource_last
            if previous is not None and at == previous:
                continue
            if previous is not None and at < previous:
                if monitor.clock() >= previous:
                    continue  # Another evaluator has already accepted this older cached sample.
                monitor.resource_state.clear()
                monitor.resource_memory.clear()
                for key, item in list(monitor.incidents.items()):
                    if key.startswith('server:') and not item.get('active'):
                        monitor.incidents.pop(key, None)
            monitor.resource_last = at
            metrics = dict(sample['metrics']) if 'metrics' in sample else values(sample)
            cores = sample.get('logical_cores') if 'metrics' in sample else capacity(sample)['logical_cores']
            metrics['load_per_core_percent'] = metrics.get('load_one') / cores * 100 if valid(cores) and cores > 0 and valid(metrics.get('load_one')) else None
            memory = metrics.get('memory_used_mb')
            if previous is not None and (at - previous > 15 or at < previous):
                monitor.resource_memory.clear()
            if valid(memory):
                monitor.resource_memory.append((at, memory))
            else:
                monitor.resource_memory.clear()
            base = next((pair for pair in reversed(monitor.resource_memory) if 55 <= at - pair[0] <= 65), None)
            metrics['memory_growth_mb'] = max(0, (memory - base[1]) * 60 / (at - base[0])) if base and valid(memory) else None
            usable = 0
            for reason, title, field, setting, unit in RULES:
                key, value, threshold = 'server:' + reason, metrics.get(field), settings[setting]
                item = monitor.incidents.get(key)
                state = monitor.resource_state.setdefault(reason, {'last': None, 'good_since': None})
                if not valid(value):
                    monitor.observe(key, None, title + '过高', '暂无有效采样', 'server')
                    state.update(last=None, good_since=None)
                    continue
                usable += 1
                if state['last'] is not None and at - state['last'] > 15:
                    state['good_since'] = None
                    if item and not item.get('active'):
                        monitor.incidents.pop(key, None)
                state['last'] = at
                # Memory growth alone during startup/cache warmup is not enough to alert.
                gate = reason != 'memory_growth' or valid(metrics.get('memory_percent')) and metrics['memory_percent'] >= 70
                bad = value >= threshold and gate
                active = bool(item and item.get('active'))
                recovering = False
                if active:
                    healthy = value <= threshold - (5 if unit == '%' else threshold * .25) or not gate
                    if healthy:
                        state['good_since'] = state['good_since'] if state['good_since'] is not None else at
                        recovering = at - state['good_since'] < settings['resource_recovery_seconds']
                        bad = recovering
                    else:
                        state['good_since'] = None
                        bad = True
                meta = {'reason': reason, 'value': value, 'threshold': threshold, 'unit': unit,
                        'sampled_at': at, 'recovering': recovering}
                if item and item.get('active') and (item.get('metadata') or {}).get('process_snapshot'):
                    meta['process_snapshot'] = item['metadata']['process_snapshot']
                elif not active and scene and scene.get('available') and abs(scene['at'] - at) <= 15:
                    # Small, immutable incident evidence stays readable after history expires.
                    meta['process_snapshot'] = frozen_scene(scene, reason)
                detail = f'当前 {value:.2f}{unit} · 阈值 {threshold}{unit}'
                if recovering:
                    detail += f" · 已回落，等待稳定 {settings['resource_recovery_seconds']} 秒确认恢复"
                monitor.observe(key, bad, title + ('过高' if reason != 'memory_growth' else ''), detail, 'server',
                    'critical' if reason in {'cpu', 'memory', 'disk', 'inode'} else 'warning', metadata=meta, observed_at=at)
            core_usable = all(valid(metrics.get(k)) for k in ('cpu_percent', 'memory_percent', 'disk_percent'))
            monitor.coverage['server'] = {'status': 'healthy' if core_usable else 'unknown',
                'detail': f'已读取 {usable}/{len(RULES)} 项资源规则；未知值不会确认恢复', 'checked_at': at}
