"""Verify the standalone public website without a server or external requests."""
from pathlib import Path
import re

import pytest

playwright = pytest.importorskip('playwright.sync_api')
WEBSITE = Path(__file__).resolve().parents[1] / 'website/index.html'


@pytest.mark.parametrize('width,height', [(1440, 1000), (1920, 1080), (390, 844), (320, 667)])
def test_website_layout_and_interactions(tmp_path, width, height):
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': height}, reduced_motion='reduce')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(WEBSITE.as_uri())
        playwright.expect(page.locator('h1')).to_have_text('mini_deploy')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('#features').bounding_box()['y'] < height
        page.screenshot(path=str(tmp_path / f'website-hero-{width}.png'))
        page.screenshot(path=str(tmp_path / f'website-{width}.png'), full_page=True)
        for name in ['requests', 'ai', 'security', 'server']:
            page.locator(f'#tab-{name}').click()
            playwright.expect(page.locator(f'#tab-{name}')).to_have_attribute('aria-selected', 'true')
            page.wait_for_function('document.querySelector("#preview-image").complete && document.querySelector("#preview-image").naturalWidth > 0')
            dimensions = page.locator('#preview-image').bounding_box()
            assert abs(dimensions['width'] / dimensions['height'] - 1.5) < .01
        page.locator('#preview-enlarge').click()
        playwright.expect(page.locator('#screenshot-dialog')).to_be_visible()
        page.keyboard.press('Escape')
        playwright.expect(page.locator('#screenshot-dialog')).to_be_hidden()
        for source in ['github', 'gitee']:
            page.locator(f'[data-repository="{source}"]').click()
            playwright.expect(page.locator('#install-code')).to_contain_text(source + '.com')
        page.locator('#copy-command').click()
        playwright.expect(page.locator('#copy-status')).to_contain_text(re.compile('已复制|已选中'))
        page.locator('.faq summary').first.click()
        assert page.locator('.faq details').first.get_attribute('open') is not None
        assert page.locator('img[src]').evaluate_all('(images) => images.every(image => image.complete && image.naturalWidth > 0)')
        assert not errors
        browser.close()
