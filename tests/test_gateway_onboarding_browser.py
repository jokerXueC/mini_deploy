"""Check consent, verification settings and collection states in the actual UI."""
import pytest

from test_onboarding_browser import browser_page as browser_page, playwright


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_new_connection_consent_and_health_expectations_are_reviewed(browser_page, tmp_path, width, theme):
    page, state = browser_page
    page.set_viewport_size({'width': width, 'height': 950})
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    page.locator('#requestsViewTab').click()
    page.locator('#gatewayConnectOpen').click()
    playwright.expect(page.locator('#connectReview')).to_be_visible()
    first = next(payload for _, payload in state['posts'] if payload.get('action') == 'preview')
    assert first['auto_restore'] is True and first['expected_status'] is None
    page.locator('#connectBack').click()
    page.locator('#connectAutoRestore').uncheck()
    page.locator('#connectOptionsAdvanced > summary').click()
    page.locator('#connectProbe').fill('/cloud/health')
    page.locator('#connectExpected').fill('204')
    page.screenshot(path=str(tmp_path / f'gateway-options-{width}-{theme}.png'), full_page=True)
    page.locator('#connectPreview').click()
    playwright.expect(page.locator('#connectReview')).to_be_visible()
    last = [payload for _, payload in state['posts'] if payload.get('action') == 'preview'][-1]
    assert last['auto_restore'] is False and last['expected_status'] == 204
    assert last['probe_path'] == '/cloud/health'
    assert not any(payload.get('action') == 'apply' for _, payload in state['posts'])
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_waiting_for_traffic_is_not_reported_as_successful_collection(browser_page):
    page, state = browser_page
    entry = {'key': 'api', 'name': 'API', 'upstream': 'http://api:8000', 'state': 'running',
             'network': 'app_default', 'local_address': '127.0.0.1:18080', 'caddy_upstream': 'mini-gateway-api:10000',
             'connection': {'state': 'connected', 'site': 'api.test', 'config_state': 'matched'},
             'collection': {'state': 'waiting', 'detail': '尚未收到真实请求；请通过原域名访问网站或调用接口'}}
    state['gateway_entries'] = [entry]
    state['gateway_requests'] = {'history': True, 'groups': [], 'total_requests': 0}
    page.locator('#requestsViewTab').click()
    playwright.expect(page.locator('#requestSource')).to_contain_text('等待真实请求')
    playwright.expect(page.locator('#requestNotice')).to_contain_text('请通过原域名访问')
    entry['collection'] = {'state': 'active', 'detail': '已收到真实请求并写入历史'}
    page.evaluate('async () => { await RequestGateway.refresh(); await NginxRequests.refresh(); }')
    playwright.expect(page.locator('#requestSource')).to_contain_text('采集中')
    playwright.expect(page.locator('#requestNotice')).to_contain_text('已收到真实请求并写入历史')


def test_missing_docker_explains_requirement_without_blocking_manual_inspection(browser_page, tmp_path):
    page, state = browser_page
    state['no_proxy'] = True
    page.route('**/gateway-connections/discover', lambda route: route.fulfill(json={
        'sources': [], 'checks': [{'label': '持久采集', 'state': 'action',
                                 'detail': '需要本机 Docker；请在安装向导选择安装，或由管理员启动 Docker 后重新检测。'}],
        'help': [{'title': '查询配置挂载', 'command': 'docker ps', 'fill': '复制一条完整指令执行'}]}))
    page.locator('#requestsViewTab').click()
    page.locator('#gatewayConnectOpen').click()
    playwright.expect(page.locator('#connectReadiness')).to_contain_text('需要本机 Docker')
    page.locator('#connectSourceAdvanced > summary').click()
    playwright.expect(page.locator('#connectContainer')).to_be_enabled()
    page.locator('#connectHelp summary').click()
    playwright.expect(page.locator('#connectHelpItems')).to_contain_text('复制一条完整指令执行')
    page.screenshot(path=str(tmp_path / 'gateway-missing-prerequisite.png'), full_page=True)


def test_disabling_proxy_trust_clears_saved_trust_without_discarding_other_options(browser_page):
    page, state = browser_page
    state['gateway_entries'] = [{
        'key': 'api', 'name': 'API', 'upstream': 'http://api:8000', 'network': 'app_default',
        'state': 'not_created', 'port': 18080, 'bind': '127.0.0.1', 'image': 'nginx:stable-alpine',
        'trust_proxy': True, 'trusted_proxies': ['172.20.0.5'], 'max_body_bytes': 0,
        'local_address': '127.0.0.1:18080', 'caddy_upstream': 'mini-gateway-api:10000', 'revision': 'old-spec'}]
    page.locator('#requestsViewTab').click()
    page.locator('#manageRequestGateways').click()
    page.locator('#gatewayMaintenance > summary').click()
    page.locator('[data-gateway-action=edit]').click()
    playwright.expect(page.locator('#gatewayTrustedProxies')).to_have_value('172.20.0.5')
    page.locator('#gatewayTrust').uncheck()
    playwright.expect(page.locator('#gatewayTrustedProxies')).to_be_disabled()
    page.locator('#gatewaySave').click()
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#requestGatewayForm')).to_be_hidden()
    saved = next(payload['spec'] for _, payload in state['posts'] if payload.get('action') == 'save')
    assert saved['trust_proxy'] is False and saved['trusted_proxies'] == [] and saved['max_body_bytes'] == 0
