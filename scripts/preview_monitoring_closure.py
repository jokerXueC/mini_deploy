"""Isolated resource-alert/Prometheus preview; never touches real host services."""
import argparse
import copy
import json
import math
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402
import monitoring  # noqa: E402
import resource_alerts  # noqa: E402
import terminal_ai  # noqa: E402
from server_performance import History  # noqa: E402
from scripts.local_ssh import configure  # noqa: E402
from scripts.preview_operations import Handler as OperationsHandler, examples  # noqa: E402


def seed(home, now=None):
    now = int(time.time() if now is None else now)
    clock = [now - 600]
    monitor = monitoring.Monitor(home / 'website-monitoring.json', clock=lambda: clock[0])
    history = History(home)
    inventory, original = examples()
    inventory['hostname'] = 'debian-demo（模拟数据）'
    for i in range(600):
        clock[0] = now - 600 + i
        server = copy.deepcopy(original['server_now'])
        server['sampled_ts'] = clock[0]
        server['cpu_percent'] = 95 if i >= 240 else 38
        server['memory']['percent'] = 93 if i >= 250 else 62
        server['memory']['used_mb'] = server['memory']['percent'] * 81.92
        server['disk_io']['iowait_percent'] = 26 if i >= 330 else 2
        history.observe(server, clock[0])
        if i % 5 == 0:
            history.record_context({'at': clock[0], 'available': True, 'scanned': 126, 'readable': 126,
                'blocked_count': 2 if i >= 330 else 0, 'zombie_count': 0, 'warnings': [],
                'scan_ms': 3.2, 'processes': [
                    {'pid': 1826, 'name': 'python', 'start_ticks': 2400, 'ppid': 1, 'uid': 1000, 'user': 'app',
                     'threads': 16, 'state': 'R', 'cpu_percent': 640 if i >= 240 else 160, 'host_cpu_percent': 80 if i >= 240 else 20,
                     'rss_mib': 2560 if i >= 250 else 850, 'memory_percent': 31.25 if i >= 250 else 10.37,
                     'cpu_change': 0, 'rss_change_mib': 0, 'read_kib_s': 428, 'write_kib_s': 1800,
                     'interval_seconds': 5, 'container': 'api', 'container_id': 'a' * 64, 'container_state': 'running',
                     'service': '', 'executable': '/usr/local/bin/python3'},
                    {'pid': 920, 'name': 'postgres', 'start_ticks': 1800, 'ppid': 1, 'uid': 999, 'user': 'postgres',
                     'threads': 1, 'state': 'D' if i >= 330 else 'S', 'cpu_percent': 88, 'host_cpu_percent': 11,
                     'rss_mib': 1024, 'memory_percent': 12.5, 'cpu_change': 0, 'rss_change_mib': 0,
                     'read_kib_s': 1200, 'write_kib_s': 4000 if i >= 330 else 300,
                     'interval_seconds': 5, 'service': 'postgresql.service', 'container': '', 'container_id': '',
                     'executable': '/usr/lib/postgresql/17/bin/postgres'}]}, server)
        resource_alerts.evaluate(monitor, [server], history.latest_scene)
        if i == 275:
            monitor.record_event('lifecycle', 'api 容器重启（模拟）', '与资源升高发生在同一时段，不能据此确认因果。', 'docker_events')
        if i == 280:
            monitor.observe('docker:api:oom', True, 'api 内存溢出退出（模拟）', '退出码 137；OOMKilled=true', 'docker', immediate=True,
                metadata={'container': 'api', 'container_id': 'a' * 64, 'oom_killed': True, 'exit_code': 137, 'reason': 'oom_killed'})
    for bucket in history.pending.values():
        bucket['metadata']['hostname'] = inventory['hostname']
    history.flush(now, force=True)
    clock[0] = now
    monitor.flush()
    monitor.clock = time.time
    return history, monitor, inventory, server


class Handler(OperationsHandler):
    def do_GET(self):  # noqa: N802
        path = agent._normal_path(urlparse(self.path).path)
        if path.startswith('/terminal-ai/') or path in {'/metrics', '/metrics/access', '/monitoring', '/operations/incident', '/status', '/system-metrics',
                    '/performance/server', '/performance/server/export', '/performance/server/context'}:
            agent.Handler.do_GET(self)
        else:
            super().do_GET()

    def do_POST(self):  # noqa: N802
        if agent._normal_path(urlparse(self.path).path) in {'/terminal-ai/panel', '/terminal-ai/cancel', '/monitoring', '/metrics/access', '/login'}:
            agent.Handler.do_POST(self)
        else:
            super().do_POST()


def main():
    parser = argparse.ArgumentParser(description='资源异常与 Prometheus 接入模拟预览')
    parser.add_argument('--port', type=int, default=60689)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--demo-ai', action='store_true', help='使用本地模拟模型，不发送数据或操作真实服务器')
    args = parser.parse_args()
    configure(args.data_dir.resolve(), args.port)
    home = Path(tempfile.mkdtemp(prefix='monitoring-demo-', dir=args.data_dir))
    history, monitor, inventory, server = seed(home)
    agent.STATE_FILE = home / 'state.json'
    agent._server_performance_instance, agent._monitor_instance = history, monitor
    agent._operations_inventory = lambda *_: copy.deepcopy(inventory)
    def realtime():
        current = copy.deepcopy(server)
        current['sampled_ts'] = time.time()
        current['cpu_percent'] = 78 + math.sin(current['sampled_ts'] / 9) * 18
        current['memory']['percent'] = 93 + math.sin(current['sampled_ts'] / 35) * 1.2
        current['memory']['used_mb'] = current['memory']['percent'] * 81.92
        return {'server': current, 'realtime_history': [], 'realtime_interval_seconds': 1}
    agent._realtime_metrics_payload = realtime
    agent._system_status_payload = lambda: {**realtime(), 'docker': {'available': True, 'containers': []}, 'history': []}
    if args.demo_ai:
        from types import SimpleNamespace
        agent._operations = lambda: SimpleNamespace(logs=lambda source, **options: {
            'source': source, 'name': 'api（模拟日志）', 'checked_at': time.time(), **options,
            'lines': ['ERROR worker exceeded memory limit; exit_code=137（模拟）'], 'truncated': False,
            'notice': '本地模拟数据，不代表真实服务器。'})
        manager = agent._ai_manager()
        manager.save({'protocol': 'openai', 'base_url': f'http://127.0.0.1:{args.port}/v1',
                      'model': '本地演示模型（模拟结果）', 'api_key': ''})
        def simulated_model(config, messages, tools=None, *, check=None, publish=None):
            for _ in range(4):
                if check:
                    check()
                time.sleep(.25)
            tool = next((m for m in reversed(messages) if m['role'] == 'tool'), None)
            data = json.loads(json.loads(tool['content'])['output'])['evidence'] if tool else {}
            rows = data.get('process_scenes') or []
            if rows and rows[0].get('run') and not any('get_panel_process_scene' in m.get('content', '') for m in messages if m['role'] == 'tool'):
                scene = rows[0]
                return {'text': '', 'calls': [{'id': 'demo_process_scene', 'type': 'function',
                    'function': {'name': 'get_panel_process_scene', 'arguments': json.dumps({'run':scene['run'], 'ts':scene['ts']})}}]}
            text = ('## 诊断结果（本地模拟）\n\n**已读取所选范围的资源与进程证据。**\n\n'
                    '| 排查线索 | 演示结果 |\n| --- | --- |\n'
                    '| CPU | Python 工作进程占用较高 |\n| 内存 | 同时段内存升高 |\n'
                    '| 容器事件 | 存在 OOM 退出与重启记录 |\n\n'
                    '这些现象发生在相近时间，**不能据此确认因果**。建议先核对工作进程并发量、容器内存限额和请求量。\n\n'
                    '本轮只读，没有修改服务。以上内容来自本地模拟模型，不是实际服务器诊断。')
            if publish:
                publish(text)
            return {'text': text, 'calls': [], 'usage': {}}
        terminal_ai.complete = simulated_model
    stop = threading.Event()
    def update():
        template = copy.deepcopy(history.latest_scene)
        while not stop.wait(1):
            current = realtime()['server']
            at = current['sampled_ts']
            history.observe(current, at)
            if at - history.latest_scene['at'] >= 5:
                scene = copy.deepcopy(template)
                scene['at'] = at
                scene['processes'][0]['cpu_percent'] = current['cpu_percent'] * 6
                scene['processes'][0]['host_cpu_percent'] = current['cpu_percent'] * .75
                history.record_context(scene, current)
            resource_alerts.evaluate(monitor, [current], history.latest_scene)
            history.flush(at)
            if at - monitor.last_flush >= 30:
                monitor.flush()
    worker = threading.Thread(target=update, daemon=True)
    worker.start()
    print(f'http://127.0.0.1:{args.port}/ui?view=events', flush=True)
    try:
        with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as http:
            http.serve_forever()
    finally:
        stop.set()
        worker.join(timeout=5)
        if agent._ai_manager_instance is not None:
            agent._ai_manager_instance.shutdown()


if __name__ == '__main__':
    main()
