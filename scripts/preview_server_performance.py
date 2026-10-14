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
    for bucket in history.pending.values():
        bucket['metadata']['hostname'] = 'debian-demo（模拟数据）'
    history.flush(now, force=True)
    return history


class Handler(OperationsHandler):
    def do_GET(self):  # noqa: N802
        path = agent._normal_path(urlparse(self.path).path)
        if path in {'/performance/server', '/performance/server/export'}:
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
