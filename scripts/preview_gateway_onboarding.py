"""Loopback-only synthetic onboarding preview; never execute gateway operations."""
import argparse
import json
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402
import gateway_connections  # noqa: E402
import request_gateway  # noqa: E402
from scripts.local_ssh import configure  # noqa: E402
from scripts.preview_request_export import Handler as ExportHandler, seed  # noqa: E402


SOURCE = {'kind': 'nginx', 'mode': 'docker', 'container': 'nginx-demo',
          'config_path': '/etc/nginx/conf.d/api.conf', 'label': 'Docker Nginx · nginx-demo（演示）'}
CONFIG = '''server {
    server_name api.demo.local www.api.demo.local;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    location /api/ { proxy_pass http://api:8080; }
}
'''


class Handler(ExportHandler):
    def do_GET(self):  # noqa: N802
        if agent._normal_path(urlparse(self.path).path) != '/gateway-connections/discover':
            return super().do_GET()
        if self._require_auth_json():
            self._write_json(200, {'sources': [SOURCE], 'errors': [],
                'help': gateway_connections.help_items(SOURCE), 'checks': [
                    {'label': '持久采集', 'state': 'ready', 'detail': '本机 Docker 可用（模拟）'},
                    {'label': '网站入口', 'state': 'ready', 'detail': '发现 1 个 Nginx 配置来源（模拟）'},
                ]})

    def do_POST(self):  # noqa: N802
        if agent._normal_path(urlparse(self.path).path) != '/gateway-connections':
            return super().do_POST()
        if not self._require_auth_json() or not self._require_csrf_json():
            return
        try:
            data = json.loads(self.rfile.read(min(int(self.headers.get('Content-Length', '0')), 16384)))
            if data.get('action') == 'inspect':
                routes = [{k: v for k, v in route.items() if not k.startswith('_')}
                          for route in gateway_connections.routes(CONFIG, 'nginx')]
                result = {'source': SOURCE, 'revision': 'demo-config', 'routes': routes,
                          'networks': ['demo_default'], 'help': gateway_connections.help_items(SOURCE)}
            elif data.get('action') == 'preview':
                spec = request_gateway.normalize({'key': data['key'], 'upstream': 'http://api:8080',
                    'network': 'demo_default', 'port': data['port'], 'trust_proxy': True,
                    'max_body_bytes': 0, 'trusted_proxies': ['172.20.0.5']})
                result = {'token': 'local-preview-only', 'gateway': spec, 'site': 'api.demo.local', 'kind': 'proxy',
                    'before_rule': 'api.demo.local /api/ → api:8080',
                    'after_rule': 'api.demo.local /api/ → mini-gateway → api:8080',
                    'notice': '本地模拟方案；不执行服务器操作。正式环境先备份和校验，再重载 Nginx。'
                              + ('已选择同一规则被覆盖时自动恢复。' if data.get('auto_restore') else '未授权后台恢复。')}
            else:
                self._write_json(403, {'detail': '本地预览仅允许检测和检查方案，不修改代理或启动容器。'})
                return
            self._write_json(200, result)
        except (KeyError, ValueError, TypeError) as exc:
            self._write_json(400, {'detail': str(exc)})


def main():
    parser = argparse.ArgumentParser(description='请求接入流程本地预览，使用模拟数据')
    parser.add_argument('--port', type=int, default=60700)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    password_path = configure(args.data_dir.resolve(), args.port)
    agent._history_instance = seed(args.data_dir.resolve())
    print(f'http://127.0.0.1:{args.port}/ui?view=requests', flush=True)
    print(f'登录密码文件：{password_path}', flush=True)
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
