"""Grouped navigation, direct links, persisted layout and mobile focus."""
import pytest

from browser_navigation import open_navigation, select_view, open_global_ai
from test_monitoring_browser import dashboard as dashboard, playwright


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (1000, 'dark'), (768, 'light'), (390, 'dark'), (320, 'light')])
def test_all_grouped_views_and_drawer_fit_without_mutations(dashboard, width, theme, tmp_path):
    page, state, origin = dashboard
    page.set_viewport_size({'width': width, 'height': 950})
    page.goto(origin + '/ui?view=server')
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    for view, category in [('server', '运行观测'), ('events', '运行观测'), ('requests', '运行观测'),
                           ('certificates', '业务管理'), ('databases', '业务管理'),
                           ('terminal', '运维安全'), ('security', '运维安全'),
                           ('notify', '面板设置'), ('setup', '面板设置')]:
        select_view(page, view)
        playwright.expect(page.locator(f'#{view}View')).to_contain_class('active')
        playwright.expect(page.locator(f'#{view}ViewTab')).to_have_attribute('aria-current', 'page')
        playwright.expect(page.locator('.navigation-group.is-open')).to_have_count(1)
        playwright.expect(page.locator('#navigationCategory')).to_have_text(category)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, view)
        assert page.locator(f'#{view}View').evaluate('el => el.scrollWidth <= el.clientWidth + 1'), (width, view)
        assert not page.locator('main.page').evaluate('el => el.inert')
        if width <= 760:
            playwright.expect(page.locator('#navigationOpen')).to_have_attribute('aria-expanded', 'false')
            assert page.locator('#panelNavigation').evaluate('el => el.inert')
    open_navigation(page)
    page.screenshot(path=str(tmp_path / f'navigation-{width}-{theme}.png'))
    assert not [call for call in state['calls'] if call[1] != 'GET']


def test_direct_link_collapsed_categories_badges_and_persisted_sidebar(dashboard):
    page, _, origin = dashboard
    page.goto(origin + '/ui?view=databases')
    playwright.expect(page.locator('#navigationBusiness')).to_have_attribute('aria-expanded', 'true')
    playwright.expect(page.locator('#navigationObserve')).to_have_attribute('aria-expanded', 'false')
    page.evaluate('PanelNavigation.updateIncidentCount(7)')
    playwright.expect(page.locator('#navigationIncidentCount')).to_have_text('7')
    playwright.expect(page.locator('#navigationIncidentCount')).to_be_visible()
    page.locator('#navigationCollapse').click()
    playwright.expect(page.locator('.panel-navigation')).to_have_css('width', '64px')
    page.reload()
    playwright.expect(page.locator('.panel-navigation')).to_have_css('width', '64px')
    page.locator('#navigationObserve').click()
    playwright.expect(page.locator('#navigationObserve')).to_have_attribute('aria-expanded', 'true')
    assert not page.locator('body').evaluate("el => el.classList.contains('navigation-collapsed')")
    playwright.expect(page.locator('#navigationIncidentCount')).to_be_hidden()
    page.locator('#navigationBusiness').click()
    page.evaluate('PanelNavigation.updateIncidentCount(0)')
    playwright.expect(page.locator('#navigationIncidentCount')).to_be_hidden()
    select_view(page, 'security')
    page.evaluate("setView('certificates')")
    playwright.expect(page.locator('#navigationBusiness')).to_have_attribute('aria-expanded', 'true')
    playwright.expect(page.locator('#navigationOperations')).to_have_attribute('aria-expanded', 'false')


def test_mobile_drawer_focus_escape_backdrop_resize_and_ai(dashboard):
    page, _, origin = dashboard
    page.set_viewport_size({'width': 390, 'height': 844})
    page.goto(origin + '/ui?view=server')
    open_navigation(page)
    playwright.expect(page.locator('#panelNavigation')).to_have_attribute('aria-modal', 'true')
    assert page.locator('main.page').evaluate('el => el.inert')
    page.locator('#globalAIOpen').focus()
    page.keyboard.press('Tab')
    playwright.expect(page.locator('.navigation-brand a')).to_be_focused()
    page.keyboard.press('Shift+Tab')
    playwright.expect(page.locator('#globalAIOpen')).to_be_focused()
    page.keyboard.press('Escape')
    playwright.expect(page.locator('#navigationOpen')).to_be_focused()
    open_navigation(page)
    page.locator('#navigationBackdrop').click(position={'x': 380, 'y': 200})
    playwright.expect(page.locator('#navigationOpen')).to_be_focused()
    open_navigation(page)
    page.set_viewport_size({'width': 1440, 'height': 1000})
    playwright.expect(page.locator('#navigationBackdrop')).to_be_hidden()
    assert not page.locator('main.page').evaluate('el => el.inert')
    assert not page.locator('#panelNavigation').evaluate('el => el.inert')
    page.set_viewport_size({'width': 390, 'height': 844})
    select_view(page, 'terminal')
    playwright.expect(page.locator('#panelTitle')).to_be_focused()
    open_global_ai(page)
    playwright.expect(page.locator('#panelAIDialog')).to_be_visible()
    playwright.expect(page.locator('#navigationBackdrop')).to_be_hidden()
    assert not page.locator('main.page').evaluate('el => el.inert')
