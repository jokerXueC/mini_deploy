"""Navigate grouped menus through the same controls used by a person."""


def open_navigation(page):
    opener = page.locator('#navigationOpen')
    if opener.is_visible() and opener.get_attribute('aria-expanded') != 'true':
        opener.click()
        page.locator('#panelNavigation').evaluate(
            'el => Promise.all(el.getAnimations().map(animation => animation.finished.catch(() => {})))')


def select_view(page, view):
    open_navigation(page)
    group = page.locator(f'[data-navigation-group]:has(#{view}ViewTab)')
    toggle = group.locator('.navigation-group-toggle')
    if toggle.get_attribute('aria-expanded') != 'true' or page.locator('body').evaluate(
            "el => el.classList.contains('navigation-collapsed')"):
        toggle.click()
    page.locator(f'#{view}ViewTab').click()


def open_global_ai(page):
    open_navigation(page)
    page.locator('#globalAIOpen').click()
