"""Generate product screenshots from real UI with isolated, synthetic API responses."""
from __future__ import annotations

import math
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright, expect  # noqa: E402
from scripts.preview_operations import examples  # noqa: E402
from scripts.preview_request_export import seed  # noqa: E402
from scripts.preview_security import examples as security_examples, login_examples  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'website' / 'assets'


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *_args):
        pass


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    inventory, evidence = examples()
    server = evidence['server_now']
    server.update(cpu={'model': 'Intel Xeon · 演示服务器', 'logical_count': 8, 'physical_cores': 4}, hostname='debian-demo')
    now = time.time()
    realtime = [{'ts': now - 59 + n, 'cpu_percent': 30 + math.sin(n / 8) * 16,
                 'memory_percent': 62 + math.sin(n / 14) * 3, 'network_total_kbps': 170 + math.sin(n / 6) * 40} for n in range(60)]
    try:
        with tempfile.TemporaryDirectory(prefix='mini-website-') as directory, sync_playwright() as runtime:
            history = seed(Path(directory))
            browser = runtime.chromium.launch()
            page = browser.new_page(viewport={'width': 1440, 'height': 960}, device_scale_factor=1)
            page.add_init_script("try { localStorage.setItem('mini_deploy-theme','light'); } catch (_) {}")
            def route(request):
                parsed = urlsplit(request.request.url)
                path = parsed.path
                if path == '/ui':
                    request.fulfill(path=str(ROOT / 'ui/index.html'), content_type='text/html')
                    return
                if path.startswith('/ui/'):
                    request.continue_()
                    return
                if path == '/status':
                    body = {'csrf_token': 'demo', 'system': {'server': server, 'realtime_history': realtime,
                            'docker': {'available': True, 'containers': [{'id': 'a' * 64, 'name': 'api-demo', 'state': 'running', 'image': 'python:3.12', 'cpu_percent': 38, 'memory_percent': 21}]}}}
                elif path == '/system-metrics':
                    body = {'server': server, 'realtime_history': realtime}
                elif path == '/monitoring':
                    body = {'targets': [], 'alerts': [evidence['incident']], 'events': evidence['timeline'], 'coverage': {}}
                elif path == '/request-gateways':
                    body = {'entries': [], 'history_sources': history.sources()}
                elif path == '/request-history':
                    query = parse_qs(parsed.query)
                    body = history.query(query.get('key', ['demo-api'])[0], layout=query.get('layout', ['tree'])[0])
                elif path == '/security/logins':
                    body = login_examples()
                    body['items'] = body['items'][:10]
                elif path in {'/security/center', '/security/fail2ban'}:
                    jail = parse_qs(parsed.query).get('jail', [''])[0]
                    body = security_examples(jail)
                    if not jail:
                        body.pop('selected')
                elif path == '/terminal-ai/settings':
                    body = {'configured': True, 'secure': True, 'model': '演示模型', 'protocol': 'openai', 'base_url': 'https://model.example/v1', 'csrf_token': 'demo'}
                elif path == '/terminal-ai/panel':
                    body = {'id': 'demo-job', 'conversation_id': 'demo-chat', 'history_id': 'demo-history', 'target': '面板主机：debian-demo'}
                elif path == '/terminal-ai/jobs':
                    body = {'status': 'done', 'model': '演示模型', 'target': '面板主机：debian-demo', 'events': [{'id': 'one', 'title': '读取所选时间段的资源与进程现场', 'status': 'done', 'result': '模拟采集结果 · CPU 高占用与内存增长'}],
                            'text': '## 排查结果（演示）\n\n**Python 工作进程占用较高。**\n\n| 证据 | 观察 |\n| --- | --- |\n| CPU | 同一时间段持续升高 |\n| 内存 | 存在明显增长 |\n| 容器事件 | 出现 OOM 退出记录 |\n\n建议核对工作进程的并发量、容器内存限额与近期请求量。\n\n这些事件时间相近，不能据此确认因果。本轮只读，未修改服务。'}
                else:
                    body = {'images': [], 'records': [], 'entries': [], 'items': [], 'profiles': [], 'sessions': [], 'targets': []}
                request.fulfill(json=body)
            page.route('**/*', route)
            origin = f'http://127.0.0.1:{http.server_port}'
            for name, view in [('server', 'server'), ('requests', 'requests'), ('security', 'security'), ('ai', 'events')]:
                page.goto(origin + '/ui?view=' + view)
                if name == 'server':
                    expect(page.locator('#cpuValue')).not_to_have_text('-')
                    page.wait_for_timeout(1000)
                elif name == 'requests':
                    expect(page.locator('#requestSampleSummary')).to_contain_text('1840')
                elif name == 'security':
                    expect(page.locator('#securityMapDots circle')).to_have_count(4)
                    expect(page.locator('#securityActivityList .security-login-row')).to_have_count(10)
                else:
                    page.evaluate("window.PanelAI.open({kind:'incident',id:'demo'})")
                    expect(page.locator('#panelAIFeedback')).to_contain_text('已保存')
                png = Path(directory) / (name + '.png')
                page.screenshot(path=str(png))
                with Image.open(png) as image:
                    image.convert('RGB').save(ASSETS / (name + '.webp'), quality=88, method=6)
                # Keep only the generated web image, never delete source or user files.
            page.goto(origin + '/ui?view=server')
            page.wait_for_timeout(1000)
            desktop = Image.open(ASSETS / 'server.webp').convert('RGB')
            hero = desktop.crop((0, 220, 1440, 960))
            hero.save(ASSETS / 'hero.webp', quality=90, method=6)
            page.set_viewport_size({'width': 390, 'height': 960})
            page.wait_for_timeout(500)
            mobile_png = Path(directory) / 'server-mobile.png'
            page.screenshot(path=str(mobile_png))
            mobile = Image.open(mobile_png).convert('RGB')
            mobile_hero = mobile.crop((12, 230, 378, 960))
            mobile_hero.save(ASSETS / 'hero-mobile.webp', quality=90, method=6)
            browser.close()
    finally:
        http.shutdown()
        http.server_close()
        worker.join(timeout=5)
    print('Website screenshots generated from the product UI with synthetic data.')


if __name__ == '__main__':
    main()
