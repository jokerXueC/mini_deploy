"""Loopback-only, read-only preview of the server workspace with synthetic samples."""
import argparse
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


def container_history(identifier, period):
    now = int(time.time() // 30) * 30
    seconds = {'1h': 3600, '24h': 86400, '7d': 604800}.get(period, 3600)
    interval = max(30, seconds // 240)
    scale = {'a': 1, 'b': .22, 'c': .06}.get(identifier[:1], .5)
    return {'container': identifier, 'period': period, 'interval_seconds': interval, 'retention_days': 7,
            'notice': '本地模拟数据 · CPU 100% = 一个逻辑核',
            'points': [{'ts': ts, 'cpu_percent': (80 + math.sin(ts / 180) * 58) * scale,
                        'memory_percent': 35 + scale * 28 + math.sin(ts / 300) * 7}
                       for ts in range(now - seconds, now + 1, interval)]}


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
                              'total_kbps': current['network_total_kbps']})
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
                self._write_json(200, {'active_count': 1, 'generated_at': time.time()})
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


def main():
    parser = argparse.ArgumentParser(description='只读服务器界面预览，所有数据为模拟数据')
    parser.add_argument('--port', type=int, default=60696)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    configure(args.data_dir.resolve(), args.port)
    Handler.demo_epoch = int(time.time())
    agent._server_performance_instance = seed(args.data_dir.resolve(), Handler.demo_epoch, recent_episode=True)
    agent._history_instance = seed_requests(args.data_dir.resolve() / 'request-demo')
    print(f'http://127.0.0.1:{args.port}/ui?view=server', flush=True)
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
