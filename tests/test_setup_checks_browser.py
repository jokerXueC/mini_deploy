"""Feature enablement navigation, small-screen layout, retries and escaping."""
import time

import pytest

import setup_checks
from test_onboarding_browser import browser_page as browser_page, playwright, ROOT


def payload(home):
    now = time.time()
    return setup_checks.snapshot(home, system={'server': {'cpu_percent': 10, 'sampled_ts': now},
        'docker': {'available': True, 'sampled_at': now, 'containers': []}},
        authenticated=True, ssh_available=True, ai={'configured': True}, notifications=False,
        protection={'phase': 'partial'}, now=now, started_at=now - 300, discovered_sites=2)


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_setup_states_instructions_and_navigation(browser_page, tmp_path, width, theme):
    page, state = browser_page
    data = payload(tmp_path)
    page.route('**/setup/checks', lambda route: route.fulfill(json=data))
    page.set_viewport_size({'width': width, 'height': 950})
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    page.locator('#setupViewTab').click()
    playwright.expect(page.locator('#setupItems .setup-row')).to_have_count(11)
    playwright.expect(page.locator('#setupSummary')).to_contain_text('2 项需处理')
    playwright.expect(page.locator('[data-setup-key=ai]')).to_have_text('配置模型')
    detail = page.locator('[data-setup-detail=requests]')
    detail.locator('summary').click()
    playwright.expect(detail).to_contain_text('自定义或无法可靠识别的配置')
    page.locator('#setupRefresh').click()
    playwright.expect(page.locator('#setupItems')).to_have_attribute('aria-busy', 'false')
    playwright.expect(detail).to_have_attribute('open', '')
    page.screenshot(path=str(tmp_path / f'feature-checks-{width}-{theme}.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not state['posts']  # Merely inspecting must never run a mutation.
    detail.locator('[data-user-guide=requests]').click()
    playwright.expect(page.locator('[data-guide-link=requests]')).to_have_attribute('aria-current', 'location')
    page.keyboard.press('Escape')
    page.locator('[data-setup-key=requests]').click()
    playwright.expect(page.locator('#requestsView')).to_have_class('dashboard-view active')


def test_failed_recheck_is_visible_and_recovers_without_losing_instructions(browser_page, tmp_path):
    page, _ = browser_page
    data = payload(tmp_path)
    fail = [False]

    def respond(route):
        route.fulfill(status=503 if fail[0] else 200, json={'detail': '暂时不可用'} if fail[0] else data)

    page.route('**/setup/checks', respond)
    page.locator('#setupViewTab').click()
    playwright.expect(page.locator('#setupItems .setup-row')).to_have_count(11)
    fail[0] = True
    page.locator('#setupRefresh').click()
    playwright.expect(page.locator('#setupFeedback')).to_contain_text('检查未完成：暂时不可用')
    playwright.expect(page.locator('#setupItems .setup-row')).to_have_count(11)
    fail[0] = False
    page.locator('#setupRefresh').click()
    playwright.expect(page.locator('#setupFeedback')).to_be_empty()


def test_deep_link_and_untrusted_error_text_are_safe(browser_page, tmp_path):
    page, _ = browser_page
    data = payload(tmp_path)
    data['items'][0]['detail'] = '<img src=x onerror="window.setupInjected=true">'
    page.route('**/setup/checks', lambda route: route.fulfill(json=data))
    # The static fixture redirects /ui?query as a directory; production serves /ui directly.
    page.route('**/ui?view=setup', lambda route: route.fulfill(
        path=str(ROOT / 'ui/index.html'), content_type='text/html'))
    page.goto(page.url + '?view=setup')
    playwright.expect(page.locator('#setupItems')).to_contain_text('<img src=x')
    assert not page.locator('#setupItems img').count()
    assert page.evaluate('window.setupInjected === undefined')
    playwright.expect(page.locator('#setupView')).to_have_class('dashboard-view setup-checks active')


def test_opening_model_settings_keeps_existing_transport_confirmation(browser_page, tmp_path):
    page, _ = browser_page
    page.route('**/setup/checks', lambda route: route.fulfill(json=payload(tmp_path)))
    page.route('**/terminal-ai/settings', lambda route: route.fulfill(json={
        'configured': False, 'protocol': 'openai', 'base_url': '', 'model': '',
        'secure': False, 'http_enabled': False, 'csrf_token': 'test'}))
    page.locator('#setupViewTab').click()
    page.locator('[data-setup-key=ai]').click()
    playwright.expect(page.locator('#aiSettingsDialog')).to_be_visible()
    playwright.expect(page.locator('#aiTest')).to_be_disabled()
    playwright.expect(page.locator('#aiSettingsFeedback')).to_contain_text('允许本次登录使用 HTTP')


def test_rechecks_update_evidence_while_preserving_keyboard_focus_and_open_instructions(browser_page, tmp_path):
    page, _ = browser_page
    data = payload(tmp_path)
    page.route('**/setup/checks', lambda route: route.fulfill(json=data))
    page.locator('#setupViewTab').click()
    summary = page.locator('[data-setup-detail=history] > summary')
    summary.click()
    summary.focus()
    history = next(item for item in data['items'] if item['key'] == 'history')
    history.update(state='verified', detail='已恢复持续写入')
    page.evaluate('SetupChecks.refresh()')
    playwright.expect(page.locator('[data-setup-row=history] .setup-state')).to_have_text('已核验')
    playwright.expect(summary).to_be_focused()
    playwright.expect(page.locator('[data-setup-detail=history]')).to_have_attribute('open', '')
