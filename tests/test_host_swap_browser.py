import pytest

from test_maintenance_performance_browser import preview as preview, playwright
from test_monitoring_browser import dashboard as dashboard


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_swap_toggle_polls_preserves_control_and_confirms_disable(preview, tmp_path, width, theme):
    page, _, _ = preview
    page.set_viewport_size({'width': width, 'height': 950})
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    state = {'active': False, 'running': False, 'finished': False, 'posts': []}

    def swap(route):
        if route.request.method == 'POST':
            assert route.request.headers['x-csrf-token'] == 'test-token'
            state['posts'].append(route.request.post_data_json)
            state.update(running=True, finished=False)
        if state['running'] and state['finished']:
            state.update(active=state['posts'][-1]['enabled'], running=False)
        route.fulfill(status=202 if route.request.method == 'POST' else 200, json={
            'supported': True, 'editable': True, 'owned': bool(state['posts']),
            'enabled': state['active'], 'managed_active': state['active'], 'persistent': state['active'],
            'detail': '开启后创建 2 GiB Swap，并设为开机生效。',
            'job': {'state': 'running' if state['running'] else 'succeeded',
                    'detail': '正在开启 Swap…' if state['running'] else '2 GiB Swap 已开启，重启后自动生效。'} if state['posts'] else {}})

    page.route('**/system/swap', swap)
    page.clock.install()
    page.locator('#performancePanel > summary').click()
    toggle = page.get_by_role('switch', name='启用 Swap')
    playwright.expect(toggle).to_be_enabled()
    playwright.expect(toggle).not_to_be_checked()
    assert toggle.get_attribute('id') == 'hostSwapToggle'
    page.locator('#hostSwapToggle').evaluate('(el) => { window.originalSwapControl = el; }')
    page.locator('#performancePanel').scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / f'swap-{width}-{theme}.png'))
    toggle.check()
    playwright.expect(toggle).to_be_disabled()
    playwright.expect(page.locator('#hostSwapFeedback')).to_have_text('正在开启 Swap…')
    state['finished'] = True
    page.clock.run_for(1800)
    playwright.expect(toggle).to_be_checked()
    playwright.expect(toggle).to_be_enabled()
    page.clock.run_for(3500)
    assert page.evaluate('window.originalSwapControl === document.getElementById("hostSwapToggle")')
    toggle.uncheck()
    playwright.expect(page.locator('#confirmModal')).to_be_visible()
    page.locator('#confirmCancelBtn').click()
    playwright.expect(toggle).to_be_checked()
    assert state['posts'] == [{'enabled': True}]
    toggle.uncheck()
    page.locator('#confirmOkBtn').click()
    playwright.expect(toggle).to_be_disabled()
    state['finished'] = True
    page.clock.run_for(1800)
    playwright.expect(toggle).not_to_be_checked()
    playwright.expect(toggle).to_be_enabled()
    assert state['posts'] == [{'enabled': True}, {'enabled': False}]
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_existing_system_swap_is_displayed_without_enabling_mutation(preview):
    page, _, _ = preview
    page.route('**/system/swap', lambda route: route.fulfill(json={
        'supported': True, 'editable': False, 'owned': False, 'enabled': True,
        'detail': '已有系统 Swap；面板保留原配置。', 'job': {}}))
    page.locator('#performancePanel > summary').click()
    toggle = page.get_by_role('switch', name='启用 Swap')
    playwright.expect(toggle).to_be_checked()
    playwright.expect(toggle).to_be_disabled()
    playwright.expect(page.locator('#hostSwapFeedback')).to_have_text('已有系统 Swap；面板保留原配置。')


def test_existing_system_swap_can_be_temporarily_disabled_and_restored(preview):
    page, _, _ = preview
    state = {'active': True, 'posts': []}

    def swap(route):
        if route.request.method == 'POST':
            state['posts'].append(route.request.post_data_json)
            state['active'] = route.request.post_data_json['enabled']
        route.fulfill(json={'supported': True, 'editable': True, 'mode': 'external',
                            'owned': False, 'enabled': state['active'], 'job': {},
                            'detail': '原交换文件和开机配置保留，重启后可能自动恢复。'})

    page.route('**/system/swap', swap)
    page.locator('#performancePanel > summary').click()
    toggle = page.get_by_role('switch', name='启用 Swap')
    playwright.expect(toggle).to_be_enabled()
    playwright.expect(toggle).to_be_checked()
    toggle.uncheck()
    playwright.expect(page.locator('#confirmModal')).to_contain_text('临时关闭系统 Swap')
    playwright.expect(page.locator('#confirmModal')).to_contain_text('重启后可能自动恢复')
    page.locator('#confirmCancelBtn').click()
    playwright.expect(toggle).to_be_checked()
    assert state['posts'] == []
    toggle.uncheck()
    page.locator('#confirmOkBtn').click()
    playwright.expect(toggle).not_to_be_checked()
    playwright.expect(toggle).to_be_enabled()
    playwright.expect(page.locator('#hostSwapRetry')).to_be_hidden()
    toggle.check()
    playwright.expect(toggle).to_be_checked()
    assert state['posts'] == [{'enabled': False}, {'enabled': True}]


def test_boot_config_retry_does_not_turn_active_swap_off(preview):
    page, _, _ = preview
    state = {'persistent': False, 'posts': []}
    def swap(route):
        if route.request.method == 'POST':
            state['posts'].append(route.request.post_data_json)
            state['persistent'] = True
        route.fulfill(json={'editable': True, 'owned': True, 'managed_active': True,
                            'enabled': True, 'persistent': state['persistent'], 'job': {}})
    page.route('**/system/swap', swap)
    page.locator('#performancePanel > summary').click()
    playwright.expect(page.locator('#hostSwapRetry')).to_be_visible()
    page.locator('#hostSwapRetry').click()
    playwright.expect(page.locator('#hostSwapRetry')).to_be_hidden()
    playwright.expect(page.locator('#hostSwapToggle')).to_be_checked()
    playwright.expect(page.locator('#confirmModal')).to_be_hidden()
    assert state['posts'] == [{'enabled': True}]
