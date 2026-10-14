"""Isolated 24-hour performance example; never represents the local OS."""
import argparse
import math
from pathlib import Path
import sys
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402
import server_performance  # noqa: E402
from scripts.local_ssh import configure  # noqa: E402
from scripts.preview_operations import Handler as OperationsHandler  # noqa: E402


def seed(home, now=None, seconds=86400):
    now = int(time.time() if now is None else now)
    history = server_performance.History(home)
    for i in range(seconds):
        if 37000 < i < 37300:  # Deliberate monitoring gap for the report.
            continue
        cpu = max(0, 30 + math.sin(i / 2400) * 16 + (60 if 44000 < i < 45000 else 0))
        server = {'cpu_percent': cpu, 'cpu': {'model': '演示 CPU', 'logical_count': 8, 'physical_cores': 4},
            'load': {'one': cpu / 18, 'five': cpu / 22, 'fifteen': cpu / 24},
            'memory': {'percent': 62 + math.sin(i / 5200) * 8, 'used_mb': 8192 * (.62 + math.sin(i / 5200) * .08),
                       'total_mb': 8192, 'swap': {'percent': 12 + math.sin(i / 8000) * 4, 'total_mb': 2048, 'used_mb': 256}},
            'disk': {'percent': 42 + i / seconds * 2, 'total_gb': 80, 'used_gb': 33.6 + i / seconds * 1.6,
                     'path': '/', 'inode': {'percent': 17 + i / seconds * 1.5}},
            'network': {'rx_kbps': 80 + abs(math.sin(i / 330)) * 260, 'tx_kbps': 45 + abs(math.sin(i / 550)) * 180},
            'disk_io': {'available': True, 'iowait_percent': 1 + abs(math.sin(i / 1000)) * 3, 'devices': [
                {'device': 'vda', 'read_kib_s': 180 + abs(math.sin(i / 430)) * 640,
                 'write_kib_s': 360 + abs(math.sin(i / 400)) * 1600, 'iops': 30 + abs(math.sin(i / 400)) * 90,
                 'await_ms': 1.2 + abs(math.sin(i / 650)) * 3.5, 'busy_percent': 15 + abs(math.sin(i / 1800)) * 40}]}}
        history.observe(server, now - seconds + i)
        if i % 30 == 0:
            busy = 44000 < i < 45000
            history.record_context({'at': now - seconds + i, 'available': True, 'scanned': 126, 'readable': 126,
                'blocked_count': 2 if busy else 0, 'zombie_count': 0, 'new_baselines': 0, 'warnings': [],
                'scan_ms': 3.6, 'processes': [
                    {'pid': 2408, 'start_ticks': 4500, 'ppid': 1, 'name': 'python', 'uid': 1000, 'user': 'app',
                     'state': 'R' if busy else 'S', 'threads': 12, 'cpu_percent': cpu * 6, 'host_cpu_percent': cpu * .75,
                     'rss_mib': 1600 if busy else 850, 'memory_percent': 19.5 if busy else 10.4,
                     'cpu_change': 36.2 if busy else .5, 'rss_change_mib': 128 if busy else 1.2,
                     'read_kib_s': 220, 'write_kib_s': 2400 if busy else 120, 'interval_seconds': 5,
                     'service': '', 'container': 'aimore-cloud', 'container_id': 'a' * 64,
                     'container_state': 'running', 'container_health': 'healthy', 'executable': '/usr/local/bin/python3'},
                    {'pid': 1832, 'start_ticks': 1300, 'ppid': 1, 'name': 'postgres', 'uid': 999, 'user': 'postgres',
                     'state': 'D' if busy else 'S', 'threads': 1, 'cpu_percent': 22.5, 'host_cpu_percent': 2.81,
                     'rss_mib': 520, 'memory_percent': 6.35, 'read_kib_s': 140, 'write_kib_s': 5600 if busy else 360,
                     'cpu_change': 4.3 if busy else -.2, 'rss_change_mib': 12 if busy else 0,
                     'interval_seconds': 5, 'service': 'postgresql.service', 'container': '',
                     'container_id': '', 'executable': '/usr/lib/postgresql/17/bin/postgres'}]}, server)
    for bucket in history.pending.values():
        bucket['metadata']['hostname'] = 'debian-demo（模拟数据）'
    history.flush(now, force=True)
    return history


class Handler(OperationsHandler):
    def do_GET(self):  # noqa: N802
        path = agent._normal_path(urlparse(self.path).path)
        if path in {'/performance/server', '/performance/server/export', '/performance/server/context'}:
            agent.Handler.do_GET(self)
        else:
            super().do_GET()


def main():
    parser = argparse.ArgumentParser(description='只读 24 小时服务器性能演示')
    parser.add_argument('--port', type=int, default=60687)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    password_path = configure(args.data_dir.resolve(), args.port)
    # Use a fresh, labelled dataset each run so examples never mix with real samples.
    import tempfile
    directory = Path(tempfile.mkdtemp(prefix='server-performance-demo-', dir=args.data_dir))
    agent.STATE_FILE = directory / 'state.json'
    agent._server_performance_instance = seed(directory)
    print(f'http://127.0.0.1:{args.port}/ui?view=server', flush=True)
    print(f'登录密码文件：{password_path}', flush=True)
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
