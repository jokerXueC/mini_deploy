"""Loopback database and global AI workspace using isolated panel state."""
import argparse
import os
from pathlib import Path
import sys
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
from scripts.local_ssh import configure  # noqa: E402


class Handler(agent.Handler):
    def send_header(self, keyword, value):
        if keyword.lower() == 'location' and value == 'ui':
            value = '/ui?view=databases'
        super().send_header(keyword, value)

    def do_GET(self):  # noqa: N802
        path = agent._normal_path(urlparse(self.path).path)
        if path.startswith(('/ui/', '/databases', '/terminal-ai/')) or path in {'/', '/ui', '/logout', '/health', '/ssh', '/ssh/output', '/ssh/stream'}:
            super().do_GET()
            return
        if not self._require_auth_json():
            return
        if path == '/status':
            self._write_json(200, {'system': {'server': {}, 'docker': {'available': False}},
                                  'agent': {'status': 'ok'}, 'csrf_token': self._csrf_token_for_request()})
        elif path == '/setup/checks':
            self._write_json(200, {'installation': {'available': False, 'commands': [],
                'detail': '本地数据库预览；Linux 服务安装命令请参考指南并在目标服务器执行。'}})
        else:
            self._write_json(200, {})

    def do_POST(self):  # noqa: N802
        path = agent._normal_path(urlparse(self.path).path)
        if path in {'/login', '/databases'} or path.startswith(('/databases/', '/terminal-ai/', '/ssh/')):
            super().do_POST()
        else:
            self.close_connection = True
            self._write_json(403, {'detail': '本地入口仅用于数据库工作区和全局 AI / SSH。'})

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description='本地数据库只读工作区，仅监听回环地址。')
    parser.add_argument('--port', type=int, default=60702)
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'mini_deploy/local-databases-v1')
    args = parser.parse_args()
    password_path = configure(args.data_dir.resolve(), args.port)
    agent._database_manager_instance = None
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        print(f'http://127.0.0.1:{args.port}/ui?view=databases', flush=True)
        print(f'本地面板密码文件：{password_path}', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
