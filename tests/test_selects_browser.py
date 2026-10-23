"""Shared select menus remain readable in filters, logs and modal dialogs."""
import pytest

from test_onboarding_browser import browser_page as browser_page, playwright


def assert_menu_fits(page, *, single_line=False):
    menu = page.locator('.form-select-menu:visible')
    playwright.expect(menu).to_have_count(1)
    bounds = menu.bounding_box()
    viewport = page.viewport_size
    assert bounds['x'] >= 11
    assert bounds['x'] + bounds['width'] <= viewport['width'] - 11
    assert bounds['y'] >= 10
    assert bounds['y'] + bounds['height'] <= viewport['height'] - 10
    assert menu.evaluate('(node) => node.scrollWidth <= node.clientWidth')
    if single_line:
        assert menu.locator('[role=option]').evaluate_all(
            '(items) => items.every(item => item.clientHeight <= 36)')
    return menu


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (768, 'dark'), (390, 'light'), (320, 'dark')])
def test_filter_menus_readable_and_aligned(browser_page, tmp_path, width, theme):
    page, _ = browser_page
    page.set_viewport_size({'width': width, 'height': 950})
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    for view, control in [('security', 'securityActivitySource'), ('events', 'incidentStatus'),
                          ('requests', 'requestStatus')]:
        page.locator(f'#{view}ViewTab').click()
        button = page.locator(f'#{control}-button')
        button.click()
        menu = assert_menu_fits(page, single_line=True)
        assert menu.bounding_box()['width'] >= 191
        page.screenshot(path=str(tmp_path / f'{view}-{width}-{theme}.png'))
        page.keyboard.press('Escape')
        playwright.expect(menu).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#securityViewTab').click()
    page.locator('#securityActivitySource-button').click()
    assert_menu_fits(page, single_line=True)
    page.keyboard.press('End')
    page.keyboard.press('Enter')
    playwright.expect(page.locator('#securityActivitySource')).to_have_value('ssh')
    playwright.expect(page.locator('#securityActivitySource-button')).to_have_text('SSH 登录')


@pytest.mark.parametrize('width', [1440, 320])
def test_long_options_and_moving_ai_panel_keep_menu_visible(browser_page, tmp_path, width):
    page, _ = browser_page
    page.set_viewport_size({'width': width, 'height': 950})
    page.route('**/terminal-ai/settings', lambda route: route.fulfill(json={
        'configured': True, 'secure': True, 'http_enabled': True,
        'protocol': 'openai', 'model': 'test-model', 'base_url': 'https://model.example/v1', 'csrf_token': 'test'}))
    page.route('**/terminal-ai/access*', lambda route: route.fulfill(json={
        'mode': route.request.post_data_json['mode'] if route.request.method == 'POST' else 'approve'}))
    page.evaluate("window.SSHTerminal.context = () => ({session_id:'mock-session', target:'test-server', cursor:0})")
    page.locator('#terminalViewTab').click()
    page.locator('#aiSettingsOpen').click()
    page.locator('#aiProtocol-button').click()
    assert_menu_fits(page)
    page.get_by_role('option', name='Anthropic / Claude 原生', exact=True).click()
    playwright.expect(page.locator('#aiProtocol')).to_have_value('anthropic')
    page.locator('#aiProtocol-button').click()
    page.keyboard.press('Escape')
    assert page.locator('#aiSettingsDialog').evaluate('(dialog) => dialog.open')
    page.locator('#aiSettingsDialog [data-ai-close]').click()
    page.locator('#aiChatOpen').click()
    page.locator('#aiPanelExpand').click()
    # This select was initialized outside the dialog before the panel moved.
    page.locator('#aiPermission-button').click()
    menu = assert_menu_fits(page, single_line=True)
    assert menu.evaluate('(node) => node.parentElement.id') == 'aiExpandedDialog'
    page.get_by_role('option', name='操作前确认', exact=True).click()
    playwright.expect(page.locator('#aiPermission')).to_have_value('approve')
    page.locator('#aiPermission-button').click()
    page.screenshot(path=str(tmp_path / f'ai-expanded-menu-{width}.png'))
    page.keyboard.press('Escape')
    assert page.locator('#aiExpandedDialog').evaluate('(dialog) => dialog.open')
    page.locator('#aiPanelExpand').click()
    page.locator('#aiPermission-button').click()
    assert_menu_fits(page)
    assert page.locator('#aiPermission-menu').evaluate('(node) => node.parentElement.tagName') == 'BODY'


@pytest.mark.parametrize('width', [1440, 320])
def test_container_log_menus_share_keyboard_controls(browser_page, tmp_path, width):
    page, _ = browser_page
    page.set_viewport_size({'width': width, 'height': 950})
    page.evaluate("document.getElementById('containerLogsModal').hidden = false")
    page.locator('#containerLogLineSelect-button').click()
    assert_menu_fits(page, single_line=True)
    page.keyboard.press('End')
    page.keyboard.press('Enter')
    playwright.expect(page.locator('#containerLogLineSelect')).to_have_value('custom')
    playwright.expect(page.locator('#containerLogLineCustomInput')).to_be_visible()
    page.locator('#containerLogDownloadSelect-button').click()
    assert_menu_fits(page, single_line=True)
    page.screenshot(path=str(tmp_path / f'container-log-menu-{width}.png'))
    page.keyboard.press('Escape')
    playwright.expect(page.locator('#containerLogsModal')).to_be_visible()
    page.locator('#closeContainerLogsBtn').click()
    playwright.expect(page.locator('.form-select-menu:visible')).to_have_count(0)


@pytest.mark.parametrize('width', [1440, 320])
def test_long_log_source_wraps_and_menu_avoids_viewport_edges(browser_page, tmp_path, width):
    page, _ = browser_page
    page.set_viewport_size({'width': width, 'height': 700})
    long_name = 'Docker / production / ' + 'very-long-service-name-' * 14
    page.evaluate('''(name) => {
      const select = document.getElementById('opsLogSource');
      select.add(new Option(name, 'long-source'));
      document.getElementById('operationsDialog').showModal();
      document.getElementById('opsPaneProcesses').hidden = true;
      document.getElementById('opsPaneLogs').hidden = false;
      window.AppSelects.syncAll();
    }''', long_name)
    page.locator('#opsLogSource-button').click()
    menu = assert_menu_fits(page)
    assert menu.get_by_role('option', name=long_name, exact=True).evaluate(
        '(node) => node.scrollWidth <= node.clientWidth')
    page.screenshot(path=str(tmp_path / f'long-log-source-{width}.png'))
    page.keyboard.press('Escape')
    page.locator('#opsLogRange-button').click()
    assert_menu_fits(page)
    page.keyboard.press('End')
    active = page.locator('#opsLogRange-button').get_attribute('aria-activedescendant')
    assert page.locator('#' + active).inner_text() == '最近 24 小时'
    page.keyboard.press('Enter')
    playwright.expect(page.locator('#opsLogRange')).to_have_value('24h')


@pytest.mark.parametrize('width', [1440, 320])
def test_compact_menu_flips_above_and_tracks_resizing(browser_page, width):
    page, _ = browser_page
    page.set_viewport_size({'width': width, 'height': 700})
    page.locator('#securityViewTab').click()
    page.locator('#securityActivitySource-button').evaluate('''(button) => {
      Object.assign(button.style, {position:'fixed', right:'12px', bottom:'12px', width:'96px', zIndex:1000});
    }''')
    button = page.locator('#securityActivitySource-button')
    button.click()
    menu = assert_menu_fits(page, single_line=True)
    assert menu.bounding_box()['y'] + menu.bounding_box()['height'] < button.bounding_box()['y']
    page.set_viewport_size({'width': width + 30, 'height': 750})
    assert_menu_fits(page, single_line=True)
    page.keyboard.press('Escape')
    playwright.expect(button).to_have_attribute('aria-expanded', 'false')
