"""Exercise authenticated Fail2ban UI against an isolated command runner."""
import pytest
import time

import agent
from test_ip_security import engine as engine
from test_ssh_browser import page as page, playwright
from test_deployment_removal import http_server as http_server, runtime as runtime
from test_security_observation import native_line
from scripts.preview_security import examples as preview_examples


@pytest.mark.parametrize('width', [1440, 390])
def test_ban_confirmations_admin_protection_and_pause(page, http_server, monkeypatch, engine, tmp_path, width):
    manager, state, _ = engine
    monkeypatch.setattr(agent, '_ip_security', lambda: manager)
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(http_server.origin + '/ui?view=security')
    playwright.expect(page.locator('#ipSecurityStatus')).to_have_text('Fail2ban 运行中')
    page.locator('#securityProtection > summary').click()
    page.get_by_role('combobox', name='现有防护规则').click()
    page.get_by_role('option', name='sshd', exact=True).click()
    playwright.expect(page.locator('#ipSecurityBanned')).to_contain_text('198.51.100.20')
    page.locator('[data-unban-ip]').click()
    page.locator('#confirmCancelBtn').click()
    assert '198.51.100.20' in state['banned']['sshd']
    page.locator('[data-unban-ip]').click()
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#ipSecurityBanned')).to_contain_text('暂无封禁')
    page.locator('#ipSecurityAddress').fill('127.0.0.1')
    page.locator('#ipSecurityBan').click()
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#ipSecurityFeedback')).to_contain_text('不能封禁当前访问 IP')
    page.locator('#ipSecurityAddress').fill('198.51.100.66')
    page.locator('#ipSecurityBan').click()
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#ipSecurityBanned')).to_contain_text('198.51.100.66')
    page.locator('#ipSecuritySetup').click()
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#ipSecuritySetup')).to_have_text('保存面板防护设置')
    playwright.expect(page.locator('#ipSecurityDisable')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'ip-security-{width}.png'), full_page=True)
    page.locator('#ipSecurityDisable').click()
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#ipSecurityDisable')).to_be_hidden()
    assert 'sshd' in state['banned']


@pytest.mark.parametrize('width', [1440, 390])
def test_ssh_discovery_history_pagination_and_preserved_settings(page, http_server, monkeypatch, engine, tmp_path, width):
    manager, state, root = engine
    state['banned'].pop('sshd')
    manager.history.log_root.mkdir()
    (manager.history.log_root / 'fail2ban.log').write_text(''.join(
        native_line(ip=f'198.51.100.{n}', fraction=f'{n:03}') for n in range(1, 62)))
    manager.scan()
    monkeypatch.setattr(agent, '_ip_security', lambda: manager)
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(http_server.origin + '/ui?view=security')
    playwright.expect(page.locator('#ipSecuritySSHInfo')).to_contain_text('2222')
    page.locator('#securityProtection > summary').click()
    playwright.expect(page.locator('#ipSecuritySSHSetup')).to_be_enabled()
    page.locator('.ip-security-settings > summary').click()
    page.locator('#ipSecuritySettingsTarget').select_option('ssh', force=True)
    page.locator('#ipSecurityIncrement').check()
    page.locator('#ipSecuritySSHSetup').click()
    playwright.expect(page.locator('#confirmMessage')).to_contain_text('2222')
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#ipSecuritySSHDisable')).to_be_visible()
    assert 'port = 2222' in (root / 'jail.d/mini-deploy-sshd.local').read_text()
    assert 'bantime.increment = true' in (root / 'jail.d/mini-deploy-sshd.local').read_text()
    page.locator('#ipSecuritySettingsTarget').select_option('panel', force=True)
    playwright.expect(page.locator('#ipSecurityIncrement')).not_to_be_checked()
    page.locator('#ipSecurityMaxretry').fill('9')
    page.locator('#ipSecuritySettingsTarget').select_option('ssh', force=True)
    playwright.expect(page.locator('#ipSecurityIncrement')).to_be_checked()
    playwright.expect(page.locator('#ipSecurityMaxretry')).to_have_value('5')
    page.locator('#ipSecuritySettingsTarget').select_option('panel', force=True)
    playwright.expect(page.locator('#ipSecurityMaxretry')).to_have_value('9')
    page.locator('.ip-security-settings > summary').click()
    page.get_by_text('Fail2ban 封禁历史', exact=True).click()
    playwright.expect(page.locator('#ipSecurityEvents .ip-security-event')).to_have_count(50)
    page.locator('#ipSecurityHistoryNext').click()
    playwright.expect(page.locator('#ipSecurityEvents .ip-security-event')).to_have_count(11)
    playwright.expect(page.locator('#ipSecurityHistoryPage')).to_contain_text('第 2 页')
    page.locator('#ipSecurityHistoryPrev').click()
    playwright.expect(page.locator('#ipSecurityEvents .ip-security-event')).to_have_count(50)
    page.get_by_text('Fail2ban 封禁历史', exact=True).click()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'ssh-security-{width}.png'), full_page=True)
    page.locator('#ipSecuritySSHDisable').click()
    page.locator('#confirmOkBtn').click()
    playwright.expect(page.locator('#ipSecuritySSHDisable')).to_be_hidden()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'light'), (320, 'dark')])
def test_security_center_risks_unified_events_filters_and_refresh(page, http_server, monkeypatch, engine, tmp_path, width, theme):
    manager, _, _ = engine
    manager.scan()
    manager.observation = preview_examples()['observation']
    monkeypatch.setattr(manager, 'request_scan', lambda: None)
    monkeypatch.setattr(agent, '_ip_security', lambda: manager)
    now = time.time()
    with manager.history.connection() as db:
        for n in range(60):
            db.execute('INSERT INTO security_activity VALUES (?,?,?,?,?,?,?,?)',
                       (str(n), now - 5 - n, 'ssh', 'login_failure', f'198.51.100.{n + 1}', 'root', 'SSH', 'SSH password 认证失败'))
        db.execute('INSERT INTO security_activity VALUES (?,?,?,?,?,?,?,?)', ('success', now - 100, 'ssh', 'login_success', '192.0.2.8', 'deploy', 'SSH', '认证成功'))
        db.execute('INSERT INTO security_activity VALUES (?,?,?,?,?,?,?,?)', ('change', now - 110, 'config', 'config_change', '', '', '/etc/crontab', '检测到内容摘要变化'))
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)', ('ban', now - 120, 'sshd', 'ban', '192.0.2.3', 'Fail2ban 封禁'))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.add_init_script(f"try {{ localStorage.setItem('mini_deploy-theme','{theme}'); }} catch (_) {{}}")
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(http_server.origin + '/ui?view=security')
    playwright.expect(page.locator('#panelTitle')).to_have_text('安全中心')
    playwright.expect(page.locator('#securityRiskCount')).to_have_text('3')
    assert page.locator('#securityProtection').get_attribute('open') is None
    playwright.expect(page.locator('#securityActivityList .security-event')).to_have_count(50)
    page.locator('#securityActivityNext').click()
    playwright.expect(page.locator('#securityActivityList .security-event')).to_have_count(13)
    page.get_by_role('combobox', name='事件来源').click()
    page.get_by_role('option', name='配置变化', exact=True).click()
    playwright.expect(page.locator('#securityActivityList .security-event')).to_have_count(1)
    playwright.expect(page.locator('#securityActivityList')).to_contain_text('/etc/crontab')
    page.locator('#securityActivityList summary').click()
    page.locator('.security-risk summary').first.click()
    page.locator('#ipSecurityRefresh').click()
    playwright.expect(page.locator('#ipSecurityRefresh')).to_be_enabled()
    assert page.locator('.security-risk').first.get_attribute('open') is not None
    assert page.locator('#securityActivityList details').get_attribute('open') is not None
    page.get_by_role('combobox', name='检查范围').click()
    page.get_by_role('option', name='容器权限', exact=True).click()
    playwright.expect(page.locator('.security-risk')).to_have_count(1)
    playwright.expect(page.locator('.security-risk')).to_contain_text('Docker 控制接口')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors
    page.screenshot(path=str(tmp_path / f'security-center-{width}-{theme}.png'), full_page=True)
