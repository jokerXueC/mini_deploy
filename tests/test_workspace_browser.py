"""Shared workspace navigation and responsive controls using read-only examples."""
import threading
from http.server import ThreadingHTTPServer

import pytest
import agent
from scripts.preview_server_overview import Handler, seed_requests
from test_ssh_browser import page as page, playwright
from test_deployment_removal import http_server as http_server, runtime as runtime


@pytest.fixture
def preview_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, '_history_instance', seed_requests(tmp_path))
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)


def test_all_views_share_workspace_and_fit_mobile_with_populated_data(page, preview_origin, tmp_path):
    # The page fixture blocks unrelated workers on the production test server.
    # This server serves only explicit synthetic examples, including monitoring.
    page.unroute('**/status')
    page.unroute('**/monitoring')
    page.add_init_script("if (window === top) localStorage.setItem('mini_deploy-theme', 'dark')")
    page.goto(preview_origin + '/ui?view=server')
    for theme in ('dark', 'light'):
        page.evaluate('(theme) => applyTheme(theme)', theme)
        for view in ('server', 'events', 'notify', 'certificates', 'requests', 'terminal', 'security'):
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.locator(f'#{view}ViewTab').click()
            playwright.expect(page.locator(f'#{view}View')).to_have_class('dashboard-view active')
            if view == 'security':
                playwright.expect(page.locator('.security-login-row')).to_have_count(10)
                playwright.expect(page.locator('#securityMapDots circle')).to_have_count(4)
            elif view == 'certificates':
                playwright.expect(page.locator('#discoverySites .discovery-row')).to_have_count(3)
            elif view == 'requests':
                playwright.expect(page.locator('#requestRows .request-group').first).to_be_visible()
            elif view == 'notify':
                page.locator('#notifyEmailEnabled').check()
            elif view == 'terminal':
                page.locator('#aiSettingsOpen').click()
                playwright.expect(page.locator('#aiSettingsDialog')).to_be_visible()
                page.keyboard.press('Escape')
            for width in (1440, 768, 390, 320):
                page.set_viewport_size({'width': width, 'height': 1000 if width > 400 else 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, view, width)
                assert page.locator(f'#{view}View').evaluate('el => el.scrollWidth <= el.clientWidth + 1'), (theme, view, width)
                assert page.locator('.hero h1').evaluate('el => getComputedStyle(el).fontSize') == ('28px' if width > 760 else '24px')
                assert page.locator('body').evaluate('el => { const grid = getComputedStyle(el, "::before"); return grid.display !== "none" && grid.backgroundImage.includes("linear-gradient") && Number(grid.opacity) > 0; }')
                if width in (1440, 390):
                    page.screenshot(path=str(tmp_path / f'{view}-{theme}-{width}.png'), full_page=True)
