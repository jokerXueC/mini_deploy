"""Loopback-only, read-only preview of the server workspace with synthetic samples."""
import argparse
import json
import math
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402
from scripts.local_ssh import configure  # noqa: E402
from scripts.preview_operations import examples  # noqa: E402
from scripts.preview_server_performance import Handler as PerformanceHandler, seed  # noqa: E402
from scripts.preview_security import Handler as SecurityHandler  # noqa: E402
from scripts.preview_request_export import Handler as RequestHandler, seed as seed_requests  # noqa: E402


def certificate_examples():
    now = time.time()
    return {'running': False, 'last_scan': now, 'progress': '本地模拟 · 已发现 3 个网站', 'items': [
        {'id': str(index), 'source': source, 'kind': kind, 'domains': [domain], 'referenced': True,
         'active': True, 'tls': True, 'can_replace': replace, 'seen_at': now,
         'config_path': '/etc/caddy/Caddyfile' if kind == 'caddy' else '/etc/nginx/conf.d/website.conf',
         'certificate_path': f'/etc/ssl/{domain}/fullchain.pem',
         'renewal': '自动续期' if kind == 'caddy' else '手动管理',
         'certificate': {'days_remaining': days, 'expires_at': now + days * 86400,
                         'not_before': now - 86400, 'issuer': "Let's Encrypt"},
         'online': {'status': 'healthy', 'checked_at': now, 'detail': 'HTTPS 200', 'matches_disk': True}}
        for index, (domain, source, kind, days, replace) in enumerate([
            ('api.example.com', 'Docker · api-gateway', 'caddy', 62, False),
            ('console.example.com', '服务器本机', 'nginx', 12, True),
            ('downloads.example.com', 'Docker · edge', 'nginx', 84, True)])]}


def container_resources(identifier, at):
    scale = {'a': 1, 'b': .22, 'c': .06}.get(identifier[:1], .5)
    wave = (1 + math.sin(at / 180)) / 2
    limit = (512 if identifier.startswith('c') else 2048) * 1024**2
    used = limit * (.38 + wave * .23)
    return {'sampled_at': at, 'cpu_limit_cores': 2, 'memory_limit_bytes': limit,
            'memory_current_bytes': used, 'memory_used_bytes': used * .85,
            'memory_headroom_bytes': limit - used, 'memory_limit_percent': used / limit * 100,
            'memory_anon_bytes': used * .7, 'memory_cache_bytes': used * .3, 'memory_swap_bytes': 0,
            'cpu_throttled_percent': wave * 26 * scale, 'cpu_limit_percent': (80 + (wave * 2 - 1) * 58) * scale / 2,
            'network_rx_bytes_s': (120 + 800 * wave) * 1024 * scale,
            'network_tx_bytes_s': (60 + 240 * wave) * 1024 * scale,
            'block_read_bytes_s': (30 + 900 * wave) * 1024 * scale,
            'block_write_bytes_s': (20 + 400 * wave) * 1024 * scale,
            'pids_current': round(12 + 70 * scale), 'pids_limit': 512, 'pids_percent': (12 + 70 * scale) / 512 * 100,
            'main_process_fds': round(20 + 140 * scale), 'main_process_fd_limit': 1024,
            'main_process_fd_percent': (20 + 140 * scale) / 1024 * 100,
            'log_bytes': (4 + 24 * scale) * 1024**2, 'log_bytes_s': (120 + 800 * wave) * scale,
            'cpu_pressure_percent': wave * 5, 'memory_pressure_percent': wave * 2,
            'io_pressure_percent': wave * 3, 'oom_kill_total': 0, 'cgroup_version': 'v2',
            'log_driver': 'json-file', 'notice': '本地模拟数据，不代表当前电脑或真实容器状态。'}


def container_history(identifier, period):
    now = int(time.time() // 30) * 30
    seconds = {'1h': 3600, '24h': 86400, '7d': 604800}.get(period, 3600)
    interval = max(30, seconds // 240)
    scale = {'a': 1, 'b': .22, 'c': .06}.get(identifier[:1], .5)
    points = []
    for index, ts in enumerate(range(now - seconds, now + 1, interval)):
        if 95 <= index < 102:
            continue  # Deliberate gap to check that the chart does not imply continuous evidence.
        point = {'ts': ts, 'cpu_percent': (80 + math.sin(ts / 180) * 58) * scale,
                 'memory_percent': 35 + scale * 28 + math.sin(ts / 300) * 7,
                 **container_resources(identifier, ts)}
        for key, value in list(point.items()):
            if key not in {'ts', 'sampled_at'} and type(value) in (int, float):
                point[key + '_peak'] = value * (1.15 if key.endswith('_s') or 'cpu' in key else 1.03)
        points.append(point)
    summary = {key: {'average': sum(p[key] for p in points) / len(points),
                     'peak': max(p[key + '_peak'] for p in points)}
               for key in points[0] if key + '_peak' in points[0]}
    current = next((row for row in system()['docker']['containers'] if row['id'] == identifier), None)
    return {'container': identifier, 'period': period, 'interval_seconds': interval, 'retention_days': 7,
            'notice': '本地模拟数据 · 实线为平均值，虚线为采样峰值；模拟缺口不连线。',
            'since': now - seconds, 'until': now, 'first_at': now - seconds, 'latest_at': now,
            'coverage_percent': 97.1, 'points': points, 'summary': summary, 'current': current, 'current_stale': False,
            'process_sampled_at': now, 'process_notice': '模拟的部分活跃进程现场，不代表完整进程列表。',
            'processes': [{'pid': 1204, 'name': 'python', 'cpu_percent': 24.8,
                           'rss_mib': 624, 'read_kib_s': 12, 'write_kib_s': 62}],
            'event_count': 2, 'events': [
                {'at_ts': now - 300, 'title': '模拟：CPU 持续限流', 'detail': '限流周期占比超过 20%，持续 60 秒。'},
                {'at_ts': now - 660, 'title': '模拟：容器已启动', 'detail': '事件与资源变化仍需按时间核对。'}]}


def sample(timestamp):
    phase = timestamp % 60
    burst = 62 * math.exp(-((phase - 36) / 2.6) ** 2)
    cpu = 24 + math.sin(timestamp / 7) * 6 + math.sin(timestamp * 1.7) * 3 + burst
    return {'ts': timestamp, 'cpu_percent': cpu, 'memory_percent': 63 + math.sin(timestamp / 29) * 3,
            'network_rx_kbps': 260 + math.sin(timestamp / 9) * 85 + burst * 6,
            'network_tx_kbps': 90 + math.sin(timestamp / 13) * 35,
            'network_total_kbps': 350 + math.sin(timestamp / 9) * 85 + math.sin(timestamp / 13) * 35 + burst * 6}


def system():
    now = int(time.time())
    _, evidence = examples()
    server = evidence['server_now']
    current = sample(now)
    server['cpu_percent'] = current['cpu_percent']
    server['cpu'].update({'model': 'AMD EPYC · 4 核 / 8 线程（模拟）', 'cores': [
        {'id': i, 'percent': max(0, min(100, current['cpu_percent'] + math.sin(now / 8 + i) * 14))}
        for i in range(8)]})
    server['memory'].update({'percent': current['memory_percent'], 'used_mb': 8192 * current['memory_percent'] / 100,
                             'available_mb': 8192 * (1 - current['memory_percent'] / 100)})
    server['network'].update({'rx_kbps': current['network_rx_kbps'], 'tx_kbps': current['network_tx_kbps'],
                              'total_kbps': current['network_total_kbps'],
                              'rx_bytes': int(18.4 * 1024**3 + (now % 86400) * 220 * 1024),
                              'tx_bytes': int(7.2 * 1024**3 + (now % 86400) * 90 * 1024)})
    containers = [
        {'id': 'a' * 64, 'name': 'api-server', 'image': 'ghcr.io/example/api:1.8.2', 'state': 'running',
         'health': 'healthy', 'cpu_percent': 27.8, 'memory_percent': 42.4, 'memory_usage': '868 MiB / 2 GiB',
         'ports': '127.0.0.1:8766 → 8766/tcp'},
        {'id': 'b' * 64, 'name': 'postgres', 'image': 'postgres:17-alpine', 'state': 'running',
         'health': 'healthy', 'cpu_percent': 6.2, 'memory_percent': 25.1, 'memory_usage': '514 MiB / 2 GiB',
         'ports': '5432/tcp'},
        {'id': 'c' * 64, 'name': 'caddy', 'image': 'caddy:2-alpine', 'state': 'running',
         'cpu_percent': 1.4, 'memory_percent': 7.3, 'memory_usage': '37 MiB / 512 MiB',
         'ports': '80, 443 → 80, 443/tcp'},
    ]
    for container in containers:
        container['resources'] = container_resources(container['id'], now)
        container['inspection'] = {'health': container.get('health', 'none'), 'restart_count': 1,
            'health_failing_streak': 0,
            'health_last_check': {'duration_ms': 4.2, 'end': '2026-11-18T08:00:00Z'}}
    return {'server': server, 'docker': {'available': True, 'containers': containers},
            'realtime_history': [sample(now - i) for i in range(60)],
            'history': [sample(now - i * 1800) for i in range(336)], 'history_interval_seconds': 1800}


class Handler(PerformanceHandler):
    demo_epoch = time.time()

    def _authenticated(self):
        # This isolated demo exposes only synthetic data and rejects every mutation.
        return True

    def do_GET(self):  # noqa: N802
        path = agent._normal_path(urlparse(self.path).path)
        if path in {'/', '/ui', '/ui/'}:
            self._write_html(200, agent._render_ui().replace('mini_deploy Monitor', 'mini_deploy · 本地模拟预览'))
        elif path == '/incident-diagnosis':
            payload = agent._diagnosis_store().snapshot(DEMO_NOTIFICATIONS)
            payload.update(target='模拟主机 · 仅供本地预览', model_configured=False, error='')
            self._write_json(200, payload)
        elif path == '/incident-diagnosis/report':
            agent.Handler.do_GET(self)
        elif path == '/notifications':
            self._write_json(200, {'notifications': agent._default_notification_config(), 'digest': {
                'next_at': 0, 'channels_ready': False, 'pending_reports': 0, 'last': None, 'deliveries': [], 'error': ''}})
        elif path == '/notifications/digest/preview':
            self._write_json(200, agent._collect_notification_digest())
        elif path in {'/observations/analyze', '/observations/requests'}:
            agent.Handler.do_GET(self)
        elif path in {'/security/center', '/security/fail2ban', '/security/logins', '/security/protection'}:
            SecurityHandler.do_GET(self)
        elif path == '/docker/logs':
            query = parse_qs(urlparse(self.path).query)
            tail = min(5000, max(1, int(query.get('tail', ['200'])[0])))
            now = int(time.time())
            lines = [time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(at)) +
                     (' WARN  slow request /cloud/health duration=1.24s' if at % 13 == 0 else ' INFO  GET /cloud/health status=200 duration=4.20ms')
                     for at in range(now - tail, now)]
            self._write_json(200, {'container': query.get('container', ['api-service'])[0], 'lines': lines})
        elif path in {'/request-history', '/request-export', '/request-gateways', '/nginx-requests', '/caddy-requests'}:
            RequestHandler.do_GET(self)
        elif path == '/certificate-discovery':
            self._write_json(200, certificate_examples())
        elif path in {'/ssh', '/terminal-ai/settings'}:
            self._write_json(200, {'available': True, 'secure': True, 'enabled': False,
                                  'profiles': [], 'sessions': [], 'configured': False})
        elif path == '/performance/containers':
            query = parse_qs(urlparse(self.path).query)
            self._write_json(200, container_history(query.get('id', ['a' * 64])[0], query.get('period', ['1h'])[0]))
        elif path == '/monitoring':
            if parse_qs(urlparse(self.path).query).get('summary') == ['1']:
                self._write_json(200, {'active_count': 1, 'unacknowledged_count': 1, 'generated_at': time.time()})
                return
            _, evidence = examples()
            events = [*evidence['timeline'],
                      {'id': 'demo-cpu', 'at_ts': self.demo_epoch - 1140, 'kind': 'alert',
                       'level': 'warning', 'title': '模拟：CPU 持续高占用', 'source': 'resource',
                       'detail': '进程现场同时记录到 python 活跃，不能仅凭时间重合认定原因。'},
                      {'id': 'demo-memory', 'at_ts': self.demo_epoch - 1140, 'kind': 'alert',
                       'level': 'warning', 'title': '模拟：内存占用超过 90%', 'source': 'resource',
                       'detail': '仅供本地交互预览，不代表当前电脑的资源状态。'}]
            self._write_json(200, {'targets': [], 'alerts': [evidence['incident']], 'events': events})
        elif path == '/status':
            self._write_json(200, {'agent': {'status': 'ok'}, 'system': system()})
        elif path == '/system-metrics':
            self._write_json(200, system())
        else:
            super().do_GET()

    def do_POST(self):  # noqa: N802
        self.close_connection = True
        self._write_json(403, {'detail': '本地演示不执行服务器操作。'})


DEMO_NOTIFICATIONS = {'wecom': {'enabled': True, 'webhook_url': 'https://example.invalid/synthetic-preview'}}


def seed_diagnosis(now):
    store = agent._diagnosis_store()
    store.configure(True)
    incident = {'id': 'synthetic-api-502', 'incident_id': 'd' * 24, 'title': '模拟：API 连续返回 502',
                'source': 'website', 'at_ts': now - 120, 'last_sample': now, 'detail': '模拟故障，不代表当前电脑状态。'}
    store.scan({'alerts': [incident]})
    if not store.detail('d' * 24)['report']:
        sections = {
            'confidence': 'suspected', 'symptom': '模拟接口在 14:02 至 14:04 返回 502，共 9 次。影响登录和任务领取。',
            'timeline': '1. 14:02:03：入口记录第一个 502。\n2. 14:02:04：应用容器出现停止事件。\n3. 14:04:12：健康探测恢复 200。',
            'investigation': '| 检查来源 | 发现 |\n|---|---|\n| 代理日志（模拟） | 上游连接被拒绝 |\n| 容器事件（模拟） | 同时存在重建记录 |\n\n日志中的连接拒绝支持上游当时不可达；容器重建的触发来源尚缺证据。',
            'cause': '**可能原因，尚待确认：** 应用重建期间上游不可达。缺少部署发起记录，不能确认谁触发了重建，也不能把正常长轮询耗时当作根因。',
            'solution': '1. 核对当前容器健康及重启次数。\n2. 对齐部署任务与错误时间。\n3. 若证实部署中断，应增加就绪检查并调整更新顺序。\n\n只读核对示例：\n```sh\ndocker ps --format "table {{.Names}}\\t{{.Status}}"\n```\n\n以上是建议，尚未执行。',
            'verification': '模拟监测已恢复。需要继续观察 5xx 和健康检查，确认之后无新增错误。恢复信号不代表已确认根因。'}
        store.running('d' * 24, 'synthetic-only')
        store.finish('d' * 24, {'status': 'done', 'text': json.dumps(sections, ensure_ascii=False),
            'events': [{'title': '模拟读取代理日志', 'status': 'done', 'command': 'read_logs', 'started_at': now - 60,
                        'result': '模拟数据：ERROR upstream connection refused'}]}, '模拟主机')
        store.queue_notifications(DEMO_NOTIFICATIONS)
    store.configure(False)


def main():
    parser = argparse.ArgumentParser(description='只读服务器界面预览，所有数据为模拟数据')
    parser.add_argument('--port', type=int, default=60696)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    configure(args.data_dir.resolve(), args.port)
    Handler.demo_epoch = int(time.time())
    agent._server_performance_instance = seed(args.data_dir.resolve(), Handler.demo_epoch, recent_episode=True)
    agent._history_instance = seed_requests(agent.STATE_FILE.parent, map_locations=True)
    seed_diagnosis(Handler.demo_epoch)
    history = agent._performance_history()
    with history.connect() as db:
        db.executemany('INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?)', (
            (key * 64, ts, name, (80 + math.sin(ts / 180) * 58) * scale,
             35 + scale * 28 + math.sin(ts / 300) * 7)
            for ts in range(int(Handler.demo_epoch // 30) * 30 - 2 * 86400,
                            int(Handler.demo_epoch // 30) * 30, 30)
            for key, name, scale in [('a', 'api-server', 1), ('b', 'postgres', .22), ('c', 'caddy', .06)]))
    print(f'http://127.0.0.1:{args.port}/ui?view=server', flush=True)
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
