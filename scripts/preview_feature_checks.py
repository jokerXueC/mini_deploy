"""Loopback-only feature checks demo using isolated, labelled sample evidence."""
import argparse
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402
import server_performance  # noqa: E402
import setup_checks  # noqa: E402
from scripts.local_ssh import configure  # noqa: E402
from scripts.preview_gateway_onboarding import Handler as GatewayHandler  # noqa: E402
from scripts.preview_request_export import seed  # noqa: E402


class Handler(GatewayHandler):
    def do_GET(self):  # noqa: N802
        path = agent._normal_path(urlparse(self.path).path)
        if path in {'/', '/ui', '/ui/'} and self._authenticated():
            self._write_html(200, agent._render_ui().replace('<body class="panel-workspace">',
                '<body class="panel-workspace"><div class="setup-demo-note" role="status">'
                '本地演示 · 模拟示例，不代表真实服务器验收</div>'))
            return
        if path != '/setup/checks':
            return super().do_GET()
        if not self._require_auth_json():
            return
        now = time.time()
        history = server_performance.History(agent.STATE_FILE.parent)
        server = {'sampled_ts': now, 'cpu_percent': 12, 'memory': {'percent': 36}}
        history.observe(server, now)
        history.flush(force=True, now=now)
        data = setup_checks.snapshot(agent.STATE_FILE.parent, system={'server': server,
            'docker': {'available': True, 'sampled_at': now, 'containers': [{}, {}, {}]}},
            authenticated=True, ssh_available=True, ai={'configured': True}, notifications=False,
            protection={'phase': 'partial'}, discovered_sites=2, now=now, started_at=now - 300)
        data['environment'] = '本地演示数据，不代表真实服务器验收'
        self._write_json(200, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=60701)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    home = args.data_dir.resolve()
    password_path = configure(home, args.port)
    agent.STATE_FILE = home / 'state.json'
    agent._history_instance = seed(home)
    print(f'http://127.0.0.1:{args.port}/ui?view=setup', flush=True)
    print(f'Password file: {password_path}', flush=True)
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
