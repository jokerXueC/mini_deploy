"""Browser regression checks for request monitoring and gateway connections."""
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import request_gateway

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def browser_page():
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def do_GET(self):
            if self.path == '/ui':
                self.path = '/ui/index.html'
            super().do_GET()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except playwright.Error:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            pytest.skip('Playwright Chromium is not installed')
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        state = {'posts': []}
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def api(route):
            path = route.request.url.split(server_url, 1)[-1].split('?')[0]
            if path.startswith('/ui'):
                route.continue_()
                return
            payload = json.loads(route.request.post_data or '{}')
            if route.request.method == 'POST':
                state['posts'].append((path, payload))
            result = {}
            if path == '/status':
                result = dict(agent={'status': 'ok'}, system={}, events=[], alerts=[], csrf_token='test')
            elif path == '/sites':
                result = dict(sites=[], notifications={})
            elif path == '/notifications':
                result = dict(notifications={})
            elif path == '/nginx-requests':
                result = state.get('nginx_requests', {'mode': 'none', 'enabled': False, 'records': [], 'notice': 'No Nginx'})
            elif path == '/caddy-requests':
                result = state.get('caddy_requests', {'mode': 'caddy', 'container': '', 'candidates': [],
                                                      'records': [], 'notice': 'No Caddy logs'})
            elif path == '/request-gateways/networks':
                result = {'networks': ['aimore_default', 'other_default']}
            elif path == '/request-gateways':
                entries = state.setdefault('gateway_entries', [])
                if route.request.method == 'POST':
                    if state.get('gateway_failure'):
                        route.fulfill(status=400, json={'detail': '测试：镜像不可用'})
                        return
                    if payload['action'] == 'save':
                        spec = request_gateway.normalize(payload['spec'])
                        entry = {**spec, 'revision': request_gateway.revision(spec), 'state': 'not_created',
                                 'local_address': f'127.0.0.1:{spec["port"]}',
                                 'caddy_upstream': f'mini-gateway-{spec["key"]}:10000'}
                        state['gateway_entries'] = [e for e in entries if e['key'] != spec['key']] + [entry]
                    elif payload['action'] == 'delete':
                        state['gateway_entries'] = [e for e in entries if e['key'] != payload['key']]
                    else:
                        entry = next(e for e in entries if e['key'] == payload['key'])
                        entry['state'] = 'running' if payload['action'] == 'start' else 'exited'
                result = {'entries': state['gateway_entries']}
            elif path in ('/gateway-requests', '/request-history'):
                result = state.get('gateway_requests', {'records': [], 'notice': '请求结束后显示记录'})
            elif path == '/gateway-connections/discover':
                result = {'sources': [] if state.get('no_proxy') else [
                    {'label': 'Docker Caddy · edge', 'kind': 'caddy', 'mode': 'docker', 'container': 'edge', 'config_path': '/etc/caddy/Caddyfile'}],
                    'help': [{'title': '查询配置挂载', 'command': 'docker inspect edge', 'fill': '配置路径填写箭头右边的容器内路径。'}]}
            elif path == '/gateway-connections':
                if state.get('connection_failure'):
                    route.fulfill(status=400, json={'detail': '配置已变化，请重新检测网站'})
                    return
                action = payload['action']
                if action == 'inspect':
                    result = {'source': payload['source'], 'revision': 'config-revision', 'networks': ['app_default'], 'routes': [
                        {'id': 'static', 'site': 'site.test', 'kind': 'static', 'label': '静态网站', 'supported': True},
                        {'id': 'api', 'site': 'api.test', 'kind': 'proxy', 'label': 'handle /cloud/*', 'upstream': 'http://api:8766', 'supported': True},
                        {'id': 'complex', 'site': 'other.test', 'kind': 'proxy', 'label': '多上游', 'supported': False, 'reason': '暂不自动改写'}],
                        'help': [{'title': '查询网络', 'command': 'docker inspect edge', 'fill': '填写代理与业务共有的网络名称。'}]}
                elif action == 'preview':
                    result = {'token': 'reviewed-token', 'gateway': {'key': payload['key']}, 'site': 'site.test', 'kind': 'static',
                              'before_rule': 'site.test → 静态文件', 'after_rule': 'site.test → mini-gateway → 原静态站点', 'notice': '确认后备份并重启 Caddy。'}
                elif action == 'apply':
                    connection = {'state': 'connected', 'site': 'site.test', 'token': 'reviewed-token'}
                    state['gateway_entries'] = [{'key': payload['key'], 'name': 'site.test', 'state': 'running', 'connection': connection,
                        'port': 18080, 'network': 'app_default', 'upstream': 'http://edge:18081', 'image': 'nginx:stable-alpine',
                        'local_address': '127.0.0.1:18080', 'caddy_upstream': 'mini-gateway-web-site-test:10000', 'revision': 'entry-revision'}]
                    result = {'connection': connection}
                elif action == 'disconnect':
                    state['gateway_entries'][0]['connection'] = {'state': 'not_connected'}
                    result = {'connection': {'state': 'not_connected'}}
            route.fulfill(json=result)

        server_url = f'http://127.0.0.1:{server.server_port}'
        page.route(server_url + '/**', api)
        page.goto(server_url + '/ui')
        try:
            yield page, state
            assert not errors, errors
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


@pytest.mark.parametrize('width', [1440, 390])
def test_request_groups_average_history_and_expansion(browser_page, width, tmp_path):
    page, state = browser_page
    page.set_viewport_size({'width': width, 'height': 950})
    spec = request_gateway.normalize({'key': 'api', 'name': 'API', 'upstream': 'http://127.0.0.1:8000'})
    state['gateway_entries'] = [{**spec, 'state': 'running', 'local_address': '127.0.0.1:18080',
                                'caddy_upstream': '127.0.0.1:18080',
                                'connection': {'state': 'connected', 'site': 'site.test', 'scope': '/cloud/*'}}]
    base = dict(at='2026-10-07T12:00:00Z', host='site.test', method='GET',
                path='/cloud/tasks', status=200, duration_ms=100, upstream_ms=None)
    records = [base, {**base, 'status': 500, 'duration_ms': 300, 'upstream_ms': 0},
               {**base, 'duration_ms': 500, 'upstream_ms': 60}]
    records += [{**base, 'path': '/cloud/health', 'status': 502 if i == 20 else 200,
                 'duration_ms': 6290, 'upstream_ms': 23.4} for i in range(65)]
    records += [{**base, 'method': 'POST'}, {**base, 'host': 'other.test'}]
    state['gateway_requests'] = {'records': records}
    page.evaluate("document.querySelector('#requestLayout').value = 'flat'")
    page.locator('#requestsViewTab').click()
    page.wait_for_selector('.request-group')
    assert page.locator('.request-group').count() == 4
    group = page.locator('.request-group').first
    assert '300.00 ms' in group.locator('summary').inner_text()
    assert '30.00 ms' in group.locator('summary').inner_text()
    assert '66.7%' in group.locator('summary').inner_text()
    assert group.locator('.request-tick').count() == 3
    health = page.locator('.request-group').nth(1)
    assert health.locator('.request-tick').count() == 30
    assert health.locator('.request-tick.is-error').count() == 1
    assert health.locator('.request-tick.is-error').bounding_box()['height'] <= 10
    assert '6.29 s' in health.locator('summary').inner_text()
    assert '98.5%' in health.locator('summary').inner_text()
    assert '70 条' in page.locator('#requestSampleSummary').inner_text()
    group.locator('summary').click()
    assert group.locator('.request-detail-row').count() == 3
    page.evaluate('window.keptRequestGroup = document.querySelector(".request-group")')
    page.locator('#requestRefresh').click()
    page.wait_for_function('!document.getElementById("requestRefresh").disabled')
    assert page.evaluate('window.keptRequestGroup === document.querySelector(".request-group")')
    assert group.get_attribute('open') is not None
    page.locator('#requestStatus').select_option('error', force=True)
    playwright.expect(page.locator('.request-group')).to_have_count(2)
    assert page.locator('.request-group').count() == 2
    assert '66.7%' in page.locator('.request-group').first.locator('summary').inner_text()
    page.locator('#requestStatus').select_option('success', force=True)
    page.wait_for_function("!document.querySelector('#requestRefresh').disabled")
    playwright.expect(page.locator('.request-group').first.locator('summary')).to_contain_text('100.0%')
    assert page.locator('.request-group').count() == 2
    page.locator('#requestStatus').select_option('all', force=True)
    playwright.expect(page.locator('.request-group')).to_have_count(4)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    for theme in ('light', 'dark'):
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        page.locator('#requestData').screenshot(path=str(tmp_path / f'request-groups-{width}-{theme}.png'), animations='disabled')


@pytest.mark.parametrize('width', [1440, 390])
def test_nginx_request_view_filters_and_escapes(browser_page, width):
    page, state = browser_page
    page.set_viewport_size({'width': width, 'height': 844})
    page.evaluate("document.querySelector('#requestLayout').value = 'flat'")
    state['nginx_requests'] = {
        'mode': 'docker', 'container': 'edge', 'enabled': True, 'notice': 'Only new requests',
        'records': [
            {'at': '2026-09-30T12:34:56+08:00', 'host': 'example.test', 'method': 'GET',
             'path': '/safe<script>alert(1)</script>', 'status': 200, 'duration_ms': 12, 'upstream_ms': 10},
            {'at': '2026-09-30T12:35:00+08:00', 'host': 'example.test', 'method': 'POST',
             'path': '/failed', 'status': 500, 'duration_ms': 33, 'upstream_ms': None},
        ],
    }
    page.locator('#requestsViewTab').click()
    page.locator('#manageRequestGateways').click()
    page.locator('#requestBackend').select_option('nginx', force=True)
    page.wait_for_selector('.request-group')
    assert page.locator('.request-group').count() == 2
    assert page.locator('#requestRows script').count() == 0
    page.locator('#requestStatus').select_option('error', force=True)
    assert page.locator('.request-group').count() == 1
    assert '/failed' in page.locator('#requestRows').inner_text()
    page.locator('#requestSearch').fill('missing')
    playwright.expect(page.locator('.request-group')).to_have_count(0)
    assert page.locator('.request-group').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


@pytest.mark.parametrize('width', [1440, 390])
def test_caddy_request_view_selects_and_filters(browser_page, width):
    page, state = browser_page
    page.set_viewport_size({'width': width, 'height': 844})
    state['caddy_requests'] = {'mode': 'caddy', 'container': 'aimore-caddy-1',
                               'candidates': ['aimore-caddy-1'], 'notice': 'Active', 'records': [
                                   {'at': '2026-09-30T06:34:56Z', 'host': 'example.test', 'method': 'GET',
                                    'path': '/ok<script>alert(1)</script>', 'status': 200,
                                    'duration_ms': 12, 'upstream_ms': None}]}
    page.locator('#requestsViewTab').click()
    page.locator('#manageRequestGateways').click()
    page.locator('#requestBackend').select_option('caddy', force=True)
    page.wait_for_selector('.request-group')
    assert page.locator('#requestBackend').input_value() == 'caddy'
    assert page.locator('#requestContainer').input_value() == 'aimore-caddy-1'
    assert page.locator('#requestEnable').is_hidden()
    assert page.locator('#requestRows script').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


@pytest.mark.parametrize('width', [1440, 390])
def test_gateway_lifecycle_and_responsive_layout(browser_page, width, tmp_path):
    page, state = browser_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.locator('#requestsViewTab').click()
    assert page.locator('#requestBackend').input_value() == 'gateway'
    page.locator('#manageRequestGateways').click()
    page.locator('#gatewayMaintenance > summary').click()
    page.locator('#gatewayAdd').click()
    page.locator('#gatewayKey').fill('aimore-api')
    page.locator('#gatewayName').fill('AimOre API <script>')
    page.locator('#gatewayUpstream').fill('http://aimore-cloud:8766')
    page.locator('#gatewayNetwork').select_option('aimore_default', force=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'gateway-form-{width}.png'), full_page=True)
    page.locator('#gatewaySave').click()
    page.locator('#confirmCancelBtn').click()
    assert not state['posts']
    page.locator('#gatewaySave').click()
    page.locator('#confirmOkBtn').click()
    page.wait_for_selector('#requestGatewayForm', state='hidden')
    page.wait_for_selector('[data-gateway-action="start"]')
    assert page.locator('#gatewayEntries script').count() == 0
    assert state['posts'][-1][1]['confirmed'] is True
    assert page.locator('#gatewayConnectOpen').is_enabled()
    page.locator('[data-gateway-action="edit"]').click()
    page.locator('#gatewayNetwork').select_option('other_default', force=True)
    page.locator('#gatewayCancel').click()
    page.locator('[data-gateway-action="start"]').click()
    page.locator('#confirmOkBtn').click()
    page.wait_for_selector('[data-gateway-action="stop"]')
    assert page.locator('[data-gateway-action="delete"]').is_disabled()
    state['gateway_requests'] = {'container': 'mini-gateway-aimore-api', 'records': [
        {'at': '2026-10-07T10:00:00+08:00', 'host': 'aimore.meetpeak.tech', 'method': 'GET',
         'path': '/cloud/health', 'status': 200, 'duration_ms': 12.4, 'upstream_ms': 11.1},
        {'at': '2026-10-07T10:00:01+08:00', 'host': 'aimore.meetpeak.tech', 'method': 'POST',
         'path': '/cloud/session', 'status': 502, 'duration_ms': 25, 'upstream_ms': None}]}
    page.locator('[data-gateway-action="records"]').click()
    page.wait_for_selector('.request-group')
    assert page.locator('.request-group:not(.request-branch)').count() == 2
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'gateway-running-{width}.png'), full_page=True)
    page.locator('[data-gateway-action="edit"]').click()
    assert page.locator('#gatewayNetwork').is_disabled()
    page.locator('#gatewayCancel').click()
    page.locator('[data-gateway-action="stop"]').click()
    page.locator('#confirmOkBtn').click()
    page.wait_for_selector('[data-gateway-action="start"]')
    page.locator('[data-gateway-action="delete"]').click()
    page.locator('#confirmOkBtn').click()
    page.wait_for_function("document.querySelectorAll('[data-gateway-action]').length === 0")
    assert [p['action'] for path, p in state['posts'] if path == '/request-gateways'] == ['save', 'start', 'stop', 'delete']


def test_gateway_save_error_keeps_form_values(browser_page):
    page, state = browser_page
    page.locator('#requestsViewTab').click()
    page.locator('#manageRequestGateways').click()
    page.locator('#gatewayMaintenance > summary').click()
    page.locator('#gatewayAdd').click()
    page.locator('#gatewayKey').fill('api')
    page.locator('#gatewayName').fill('API')
    page.locator('#gatewayUpstream').fill('http://127.0.0.1:8000')
    state['gateway_failure'] = True
    page.locator('#gatewaySave').click()
    page.locator('#confirmOkBtn').click()
    page.wait_for_function("document.querySelector('#gatewayFeedback').textContent.includes('操作失败')")
    assert page.locator('#requestGatewayForm').is_visible()
    assert page.locator('#gatewayUpstream').input_value() == 'http://127.0.0.1:8000'
    assert page.locator('#gatewaySave').is_enabled()


@pytest.mark.parametrize('width', [1440, 390])
def test_connection_wizard_preview_apply_and_disconnect(browser_page, width, tmp_path):
    page, state = browser_page
    page.set_viewport_size({'width': width, 'height': 950})
    page.locator('#requestsViewTab').click()
    assert page.locator('#requestBackend').is_hidden()
    assert page.locator('#gatewayAdd').is_hidden()
    page.locator('#gatewayConnectOpen').click()
    page.wait_for_function("document.querySelector('#connectContainer').value === 'edge'")
    page.wait_for_selector('#connectReview', state='visible')
    assert page.locator('#connectContainer').is_hidden()
    assert page.locator('#connectPort').is_hidden()
    assert '/cloud/*' in page.locator('#connectReviewScope').inner_text()
    page.locator('#connectBack').click()
    page.locator('#connectSourceAdvanced > summary').click()
    page.locator('#connectIncludeStatic').check()
    page.locator('#connectService').select_option('0', force=True)
    page.wait_for_selector('#connectReview', state='visible')
    page.locator('#connectBack').click()
    assert page.locator('#connectNetwork').input_value() == 'app_default'
    page.locator('#connectOptionsAdvanced > summary').click()
    assert page.locator('#connectInternalField').is_visible()
    page.locator('#connectPort').fill('18081')
    assert page.locator('#connectInternal').input_value() != '18081'
    assert page.locator('#connectRoute option[value="complex"]').evaluate('(option) => option.disabled')
    page.locator('#connectHelp summary').click()
    assert '共有的网络' in page.locator('#connectHelpItems').inner_text()
    page.locator('#connectPreview').click()
    page.wait_for_selector('#connectReview', state='visible')
    assert not any(p.get('action') == 'apply' for _, p in state['posts'])
    page.locator('#connectBack').click()
    page.locator('#connectPort').fill('18083')
    assert page.locator('#connectReview').is_hidden()
    page.locator('#connectPreview').click()
    page.wait_for_selector('#connectReview', state='visible')
    page.locator('#connectHelp summary').click()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'connection-review-{width}.png'), full_page=True)
    page.locator('#gatewayConnectClose').click()
    assert not any(p.get('action') == 'apply' for _, p in state['posts'])
    page.locator('#gatewayConnectOpen').click()
    playwright.expect(page.locator('#connectService option')).to_have_count(4)
    page.locator('#connectService').select_option('0', force=True)
    page.wait_for_selector('#connectReview', state='visible')
    page.locator('#connectApply').click()
    page.wait_for_selector('#requestStopMonitoring', state='visible')
    assert page.locator('#gatewayConnectPanel').is_hidden()
    assert 'site.test' in page.locator('#requestSource').inner_text()
    assert page.locator('[data-gateway-action="edit"]').is_disabled()
    assert page.locator('[data-gateway-action="stop"]').is_disabled()
    apply = next(p for _, p in state['posts'] if p.get('action') == 'apply')
    assert apply['confirmed'] is True and apply['token'] == 'reviewed-token'
    page.locator('#requestStopMonitoring').click()
    page.locator('#confirmOkBtn').click()
    page.wait_for_selector('[data-gateway-action="disconnect"]', state='detached')
    page.wait_for_selector('#requestStopMonitoring', state='hidden')


def test_connection_errors_preserve_inputs_and_show_help(browser_page):
    page, state = browser_page
    state['no_proxy'] = True
    page.locator('#requestsViewTab').click()
    page.locator('#manageRequestGateways').click()
    page.locator('#gatewayConnectOpen').click()
    page.wait_for_function("!document.querySelector('#gatewayConnectFields').disabled")
    page.locator('#connectSourceAdvanced > summary').click()
    page.locator('#connectHelp summary').click()
    assert '右边' in page.locator('#connectHelpItems').inner_text()
    page.locator('#connectContainer').fill('custom-edge')
    state['connection_failure'] = True
    page.locator('#connectInspect').click()
    page.wait_for_function("document.querySelector('#gatewayConnectFeedback').textContent.includes('配置已变化')")
    assert page.locator('#connectContainer').input_value() == 'custom-edge'
    assert page.locator('#connectHelp').get_attribute('open') is not None
    assert page.locator('#connectInspect').is_enabled()
