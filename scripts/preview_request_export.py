"""Isolated synthetic request history for local export and report previews."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402
import request_history  # noqa: E402
from scripts.local_ssh import configure  # noqa: E402
from scripts.preview_operations import Handler as OperationsHandler  # noqa: E402


def seed(directory, now=None):
    now = time.time() if now is None else now
    history = request_history.History(directory)
    for key, name, total in [('demo-api', 'API 服务（演示）', 1840), ('demo-web', '网站（演示）', 640)]:
        lines = []
        for index in range(total):
            ts = now - 86400 + (index + 1) / total * 86000
            at = datetime.fromtimestamp(ts, timezone.utc).isoformat().replace('+00:00', 'Z')
            route = ['/cloud/health', '/cloud/runtime/sessions/' + str(100 + index % 40),
                     '/cloud/chat', '/cloud/files', '/login', '/api/tasks/' + str(index % 12)][index % 6]
            method = 'POST' if route in {'/cloud/chat', '/login'} else 'GET'
            duration = .008 + (index % 19) * .007 + (1.9 if index % 29 == 0 else 0)
            status = 503 if index % 31 == 0 else 404 if index % 23 == 0 else 200
            item = {'at': at, 'host': 'api.demo.local' if key == 'demo-api' else 'demo.local',
                'method': method, 'path': route, 'status': str(status), 'duration': str(duration),
                'upstream': str(duration * .83), 'client_ip': f'203.0.113.{1 + index % 24}',
                'protocol': 'HTTP/2.0', 'response_bytes': str(128 + index % 4096),
                'request_id': f'demo-{key}-{index}', 'upstream_address': '127.0.0.1:8766',
                'request_headers': {'User-Agent': 'mini_deploy export demo'}}
            lines.append(at + ' mini_deploy_req ' + json.dumps(item))
        history.ingest(key, name, 'demo-' + str(int(now)), lines, now, now=now)
    return history


class Handler(OperationsHandler):
    def do_GET(self):  # noqa: N802
        path = agent._normal_path(urlparse(self.path).path)
        if path in {'/request-history', '/request-export'}:
            agent.Handler.do_GET(self)
        elif path == '/request-gateways':
            if self._require_auth_json():
                self._write_json(200, {'entries': [], 'history_sources': agent._request_history().sources()})
        elif path in {'/nginx-requests', '/caddy-requests'}:
            if self._require_auth_json():
                self._write_json(200, {'records': [], 'notice': '演示入口请使用后端请求历史。'})
        else:
            super().do_GET()


def main():
    parser = argparse.ArgumentParser(description='请求导出本地演示，使用独立模拟数据')
    parser.add_argument('--port', type=int, default=60686)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    password_path = configure(args.data_dir.resolve(), args.port)
    agent.STATE_FILE = args.data_dir.resolve() / 'requests-demo' / 'state.json'
    agent._history_instance = seed(agent.STATE_FILE.parent)
    print(f'http://127.0.0.1:{args.port}/ui?view=requests', flush=True)
    print(f'登录密码文件：{password_path}', flush=True)
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
