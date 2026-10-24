"""Exercise the native login workspace without installing Fail2ban or changing host rules."""
import json
from types import SimpleNamespace

import pytest

import agent
import basic_protection
from test_login_guard import guard as guard
from login_guard import DEFAULTS
from test_ssh_browser import page as page, playwright
from test_deployment_removal import http_server as http_server, runtime as runtime


@pytest.fixture
def native(guard, monkeypatch):
    engine, now, kernel = guard
    for index in range(23):
        source = 'panel' if index % 3 == 0 else 'ssh'
        with engine.history.connection() as db:
            engine._event(db, source, 'login_success' if index % 3 == 0 else 'login_failure',
                          '8.8.8.8' if index % 2 else '1.1.1.1', '<script>not markup</script>',
                          now[0] - index * 42)
    with engine.history.connection() as db:
        for ip, lat, lon in [('8.8.8.8', 31.23, 121.47), ('1.1.1.1', 35.68, 139.69)]:
            db.execute('INSERT INTO login_guard_geo VALUES (?,?,?)',
                       (ip, now[0], json.dumps({'label': '测试 · 地区', 'latitude': lat, 'longitude': lon})))
    monkeypatch.setattr(agent, '_ip_security', lambda: SimpleNamespace(guard=engine, observation={'ssh': {}}))
    protection = basic_protection.Protection(engine.history.path.parent.parent, engine,
        discovery=lambda: ([], [], []))
    monkeypatch.setattr(agent, '_basic_protection', lambda: protection)
    return engine, now, kernel


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'light'), (320, 'dark')])
def test_login_map_real_records_filter_pagination_theme_and_retained_errors(page, http_server, native, tmp_path, width, theme):
    page.set_viewport_size({'width': width, 'height': 1000})
    page.add_init_script('try { localStorage.setItem("mini_deploy-theme", ' + json.dumps(theme) + ') } catch (_) {}')
    page.goto(http_server.origin + '/ui?view=security')
    playwright.expect(page.locator('#securityActivityList .security-login-row')).to_have_count(10)
    playwright.expect(page.locator('#securityMapLand path').first).to_be_visible()
    playwright.expect(page.locator('#securityMapDots circle')).to_have_count(2)
    assert page.locator('#securityCoverage, #securityRisks, #ipSecurityJail').count() == 0
    assert page.locator('body').evaluate('el => el.scrollWidth <= innerWidth')
    assert page.locator('#securityMapDots circle').first.evaluate('el => getComputedStyle(el).animationName') == 'none'
    assert page.locator('#securityMap').evaluate('el => el.getBoundingClientRect().height') > 100
    assert page.locator('#securityMapLand path').first.evaluate('el => el.getBBox().width') > 1
    page.locator('#securityActivityNext').click()
    playwright.expect(page.locator('#securityActivityPage')).to_contain_text('第 2 / 3 页')
    page.locator('#securityActivityNext').click()
    playwright.expect(page.locator('#securityActivityList .security-login-row')).to_have_count(3)
    playwright.expect(page.locator('#securityActivityNext')).to_be_disabled()
    page.get_by_role('combobox', name='结果', exact=True).click()
    page.get_by_role('option', name='登录成功', exact=True).click()
    playwright.expect(page.locator('#securityActivityList .security-login-row')).to_have_count(8)
    playwright.expect(page.locator('#securityActivityPage')).to_contain_text('第 1 / 1 页')
    assert page.locator('#securityActivityList script').count() == 0
    page.locator('#securityActivityList summary').first.click()
    playwright.expect(page.locator('#securityActivityList details').first).to_contain_text('<script>not markup</script>')
    page.route('**/security/logins?*', lambda route: route.fulfill(status=503, json={'detail': '临时读取失败'}))
    page.locator('#ipSecurityRefresh').click()
    playwright.expect(page.locator('#securitySummaryNote')).to_contain_text('临时读取失败')
    playwright.expect(page.locator('#securityActivityList .security-login-row')).to_have_count(8)
    playwright.expect(page.locator('#securityGuardToggle')).to_be_disabled()
    page.screenshot(path=str(tmp_path / ('login-security-' + str(width) + '-' + theme + '.png')), full_page=True)


def test_policy_draft_survives_refresh_and_save_protects_current_client(page, http_server, native):
    engine, _, _ = native
    page.goto(http_server.origin + '/ui?view=security')
    playwright.expect(page.locator('#securityGuardToggle')).to_be_enabled()
    assert not page.locator('#securityPolicyForm').is_visible()
    page.locator('#securityPolicyOpen').click()
    page.locator('#securityMaxretry').fill('7')
    page.locator('#securityIgnore').fill('192.168.0.0/16')
    page.locator('#ipSecurityRefresh').click()
    playwright.expect(page.locator('#ipSecurityRefresh')).to_be_enabled()
    assert page.locator('#securityMaxretry').input_value() == '7'
    page.locator('#securityPolicyForm button[type=submit]').click()
    playwright.expect(page.locator('#ipSecurityFeedback')).to_have_text('已保存。')
    playwright.expect(page.locator('#securityPolicySummary')).to_contain_text('失败 7 次')
    settings = engine.status()['settings']
    assert '127.0.0.1/32' in settings['ignoreip'] and '192.168.0.0/16' in settings['ignoreip']
    assert settings['maxretry'] == 7
    page.locator('#securityGuardToggle').click()
    playwright.expect(page.locator('#confirmModal')).to_be_visible()
    page.locator('#confirmCancelBtn').click()
    assert engine.status()['settings']['enabled']
    page.locator('#securityGuardToggle').click()
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#securityGuardToggle')).to_have_attribute('aria-checked', 'false')
    assert not engine.status()['settings']['enabled']


def test_auto_refresh_displays_new_logins_and_unban_preserves_records(page, http_server, native):
    engine, now, _ = native
    page.clock.install()
    page.goto(http_server.origin + '/ui?view=security')
    playwright.expect(page.locator('#securityActivityList .security-login-row')).to_have_count(10)
    before = engine.status()['total']
    now[0] += 1
    engine.record_panel('9.9.9.9', False)
    page.clock.fast_forward(5100)
    playwright.expect(page.locator('#securityActivityList')).to_contain_text('9.9.9.9')
    playwright.expect(page.locator('#securityActivityPage')).to_contain_text('共 24 条')
    with engine.history.connection() as db:
        db.execute('INSERT INTO login_guard_bans VALUES (?,?,?,?,?)', ('9.9.9.9', 'panel', now[0], now[0] + 3600, '连续失败'))
    page.locator('#ipSecurityRefresh').click()
    playwright.expect(page.locator('#securityBans [data-unban="9.9.9.9"]')).to_have_count(1)
    page.locator('#securityBans [data-unban="9.9.9.9"]').click()
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#securityBans [data-unban="9.9.9.9"]')).to_have_count(0)
    assert engine.status()['total'] == before + 1


@pytest.mark.parametrize('width,theme', [(1440, 'dark'), (390, 'light'), (320, 'dark')])
def test_map_point_details_group_ips_counts_keyboard_and_refresh(page, http_server, native, tmp_path, width, theme):
    engine, now, _ = native
    with engine.history.connection() as db:
        db.execute('UPDATE login_guard_geo SET value=?',
                   (json.dumps({'label': '测试 · 同一地区', 'latitude': 31.23, 'longitude': 121.47}),))
    page.set_viewport_size({'width': width, 'height': 900})
    page.add_init_script('try { localStorage.setItem("mini_deploy-theme", ' + json.dumps(theme) + ') } catch (_) {}')
    page.clock.install()
    page.goto(http_server.origin + '/ui?view=security')
    point = page.locator('#securityMapDots [role=button]')
    playwright.expect(point).to_have_count(1)
    playwright.expect(page.locator('#securityMapDetail')).to_be_hidden()
    point.click()
    playwright.expect(page.locator('#securityMapDetailTitle')).to_have_text('测试 · 同一地区')
    playwright.expect(page.locator('#securityMapDetailBody')).to_contain_text('2 个 IP · 23 次登录')
    rows = page.locator('.security-map-ip')
    playwright.expect(rows).to_have_count(2)
    playwright.expect(rows.nth(0)).to_contain_text('1.1.1.1')
    playwright.expect(rows.nth(0).locator('b')).to_have_text('12 次')
    playwright.expect(rows.nth(0).locator('p')).to_have_text('成功 4失败 8')
    playwright.expect(rows.nth(1)).to_contain_text('8.8.8.8')
    playwright.expect(rows.nth(1).locator('b')).to_have_text('11 次')
    playwright.expect(rows.nth(1).locator('p')).to_have_text('成功 4失败 7')
    playwright.expect(page.locator('#securityRegions')).to_be_hidden()
    point.focus()
    point.evaluate('el => { window.originalMapPoint = el; }')
    now[0] += 1
    engine.record_panel('1.1.1.1', True)
    page.clock.fast_forward(5100)
    playwright.expect(page.locator('#securityMapDetailBody')).to_contain_text('24 次登录')
    playwright.expect(rows.nth(0).locator('p')).to_have_text('成功 5失败 8')
    assert point.evaluate('el => el === window.originalMapPoint && document.activeElement === el')
    assert page.locator('body').evaluate('el => el.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / ('map-detail-' + str(width) + '-' + theme + '.png')), full_page=True)
    page.keyboard.press('Escape')
    playwright.expect(page.locator('#securityMapDetail')).to_be_hidden()
    playwright.expect(page.locator('#securityRegions')).to_be_visible()
    playwright.expect(point).to_be_focused()
    page.keyboard.press('Enter')
    playwright.expect(page.locator('#securityMapDetail')).to_be_visible()
    page.locator('#securityMapDetailClose').click()
    playwright.expect(point).to_be_focused()
    page.keyboard.press('Space')
    playwright.expect(page.locator('#securityMapDetail')).to_be_visible()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_map_ip_manual_ban_confirmation_actual_enforcement_and_unban(
        page, http_server, native, monkeypatch, tmp_path, width, theme):
    engine, _, kernel = native
    engine.change({**DEFAULTS, 'action': 'settings', 'maxretry': 50}, '127.0.0.1')
    engine.tick({'ready': True, 'ports': [2222]})
    monkeypatch.setattr(agent, '_ip_security', lambda: SimpleNamespace(
        guard=engine, observation={'ssh': {'ready': True, 'ports': [2222]}}))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.add_init_script('try { localStorage.setItem("mini_deploy-theme", ' + json.dumps(theme) + ') } catch (_) {}')
    page.goto(http_server.origin + '/ui?view=security')
    page.locator('#securityMapDots [role=button]').first.click()
    button = page.locator('#securityMapDetailBody [data-ban]').first
    playwright.expect(button).to_be_enabled()
    ip = button.get_attribute('data-ban')
    button.click()
    playwright.expect(page.locator('#confirmMessage')).to_contain_text(ip)
    playwright.expect(page.locator('#confirmMessage')).to_contain_text('面板和 SSH 登录')
    playwright.expect(page.locator('#confirmMessage')).to_contain_text('1 小时')
    page.locator('#confirmCancelBtn').click()
    assert engine.status()['ban_total'] == 0
    button.click()
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#ipSecurityFeedback')).to_have_text('封禁已生效，到期自动解封。')
    playwright.expect(button).to_have_text('已封禁')
    playwright.expect(button).to_be_disabled()
    assert engine.blocked(ip) and all(row['enforced'] for row in engine.status()['bans'])
    assert kernel.calls[-1][1][0]['ip'] == ip
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#securityMapDetail').scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / f'map-manual-ban-{width}-{theme}.png'))
    for scope in ('panel', 'ssh'):
        page.locator(f'#securityBans [data-unban="{ip}"][data-source="{scope}"]').click()
        page.locator('#confirmOkBtn').click()
        playwright.expect(page.locator(f'#securityBans [data-unban="{ip}"][data-source="{scope}"]')).to_have_count(0)
    playwright.expect(button).to_be_enabled()
    assert not engine.blocked(ip) and engine.status()['ban_total'] == 0


def test_manual_ban_failure_does_not_show_success_and_current_ip_is_disabled(page, http_server, native, monkeypatch):
    monkeypatch.setattr(agent, '_request_client_ip', lambda request: '1.1.1.1')
    page.goto(http_server.origin + '/ui?view=security')
    for point in page.locator('#securityMapDots [role=button]').all():
        point.click()
        if page.locator('#securityMapDetailBody [data-ban="1.1.1.1"]').count():
            break
    playwright.expect(page.locator('#securityMapDetailBody [data-ban="1.1.1.1"]')).to_be_disabled()
    for point in page.locator('#securityMapDots [role=button]').all():
        point.click()
        if page.locator('#securityMapDetailBody [data-ban="8.8.8.8"]').count():
            break
    page.route('**/security/logins', lambda route: route.fulfill(status=503, json={'detail': '内核拒绝封禁'}))
    button = page.locator('#securityMapDetailBody [data-ban="8.8.8.8"]')
    button.click()
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#ipSecurityFeedback')).to_contain_text('内核拒绝封禁')
    playwright.expect(button).to_have_text('手动封禁')
    playwright.expect(button).to_be_enabled()
    assert native[0].status()['ban_total'] == 0


def test_basic_protection_click_progress_failure_and_explicit_coverage(page, http_server, native, tmp_path):
    state = {'enabled': False, 'phase': 'off', 'entries': [], 'errors': [], 'busy': False, 'csrf_token': 'test'}
    posts = []

    def route_api(route):
        if route.request.method == 'POST':
            posts.append(route.request.post_data_json)
            state.update(enabled=True, busy=True, phase='applying')
            route.fulfill(status=202, json=state)
        else:
            route.fulfill(json=state)

    page.route('**/security/protection', route_api)
    page.clock.install()
    page.goto(http_server.origin + '/ui?view=security')
    toggle = page.locator('#securityBasicToggle')
    playwright.expect(toggle).to_be_enabled()
    assert not page.locator('#securityBasicDetails').evaluate('el => el.open')
    toggle.click()
    playwright.expect(toggle).to_be_disabled()
    playwright.expect(page.locator('#securityBasicSummary')).to_contain_text('正在检测并应用')
    assert posts == [{'enabled': True}]
    state.update(busy=False, phase='on', connections={'state': 'active', 'detail': 'TCP 新连接限制'},
                 entries=[{'state': 'unavailable', 'label': 'Caddy', 'detail': '无内置请求限流，原配置保留'}])
    page.clock.fast_forward(5100)
    playwright.expect(toggle).to_be_enabled()
    playwright.expect(page.locator('#securityBasicSummary')).to_contain_text('部分能力未接入')
    page.locator('#securityBasicDetails summary').click()
    playwright.expect(page.locator('#securityBasicCoverage')).to_contain_text('无内置请求限流')
    for width, theme in [(1440, 'dark'), (320, 'light')]:
        page.set_viewport_size({'width': width, 'height': 900})
        page.evaluate('(theme) => applyTheme(theme)', theme)
        page.clock.run_for(350)
        assert page.locator('body').evaluate('el => el.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / ('basic-coverage-' + str(width) + '.png')), full_page=True, animations='disabled')
    page.route('**/security/protection', lambda route: route.fulfill(status=503, json={'detail': '状态暂不可读'}))
    page.clock.fast_forward(5100)
    playwright.expect(page.locator('#securityBasicFeedback')).to_contain_text('状态暂不可读')
    playwright.expect(toggle).to_be_disabled()
    playwright.expect(toggle).to_have_attribute('aria-checked', 'true')
