"""Verify the standalone public website without a server or external requests."""
from pathlib import Path
import re

import pytest

playwright = pytest.importorskip('playwright.sync_api')
WEBSITE = Path(__file__).resolve().parents[1] / 'website/index.html'


@pytest.mark.parametrize('width,height', [(1440, 1000), (1920, 1080), (1024, 768), (768, 900), (390, 844), (320, 667)])
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
        demo = page.frame_locator('#demo-panel')
        playwright.expect(demo.locator('.status-stat-card')).to_have_count(4)
        assert page.locator('#demo-panel').bounding_box()['height'] > 100
        assert demo.locator('body').evaluate('() => document.documentElement.scrollWidth <= innerWidth')
        # Product styling uses continuous separators and sixty-cell resource tracks.
        assert demo.locator('.status-stat-card').first.evaluate('el => getComputedStyle(el).borderRadius') == '0px'
        assert demo.locator('.server-status-grid').evaluate('el => getComputedStyle(el).display') == 'grid'
        assert demo.locator('#cpuValue').evaluate('el => parseFloat(getComputedStyle(el).fontSize)') >= 27
        playwright.expect(demo.locator('#cpuBar .resource-bar')).to_have_count(60)
        assert page.locator('[data-repository="github"]').get_attribute('aria-pressed') == 'true'
        page.screenshot(path=str(tmp_path / f'website-hero-{width}.png'))
        page.screenshot(path=str(tmp_path / f'website-{width}.png'), full_page=True)
        for name in ['requests', 'ai', 'security', 'server']:
            page.locator(f'#tab-{name}').click()
            playwright.expect(page.locator(f'#tab-{name}')).to_have_attribute('aria-selected', 'true')
            page.wait_for_function('document.querySelector("#preview-image").complete && document.querySelector("#preview-image").naturalWidth > 0')
            dimensions = page.locator('#preview-image').bounding_box()
            assert abs(dimensions['width'] / dimensions['height'] - 1.5) < .01
        page.locator('#tab-server').focus()
        page.keyboard.press('ArrowRight')
        playwright.expect(page.locator('#tab-requests')).to_be_focused()
        playwright.expect(page.locator('#preview-number')).to_have_text('02 / 04')
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


@pytest.mark.parametrize('width', [1440, 390])
def test_resource_replay_moves_pauses_scrubs_and_stops_when_hidden(tmp_path, width):
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 960})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url))
        page.goto(WEBSITE.as_uri())
        page.locator('#signal-canvas').scroll_into_view_if_needed()
        page.wait_for_function('Number(document.querySelector("#signal-canvas").dataset.step) > 0')
        canvas = page.locator('#signal-canvas')
        first_frame = int(canvas.get_attribute('data-frame'))
        page.wait_for_function('(first) => Number(document.querySelector("#signal-canvas").dataset.frame) > first + 2', arg=first_frame)
        # Pixel check catches a blank canvas even if its animation callbacks run.
        assert canvas.evaluate('''el => {
            const pixels = el.getContext('2d').getImageData(0,0,el.width,el.height).data;
            let painted = 0; for (let i=3;i<pixels.length;i+=4) if (pixels[i]) painted++;
            return painted > el.width * el.height * .02;
        }''')
        page.locator('#signal-pause').click()
        playwright.expect(page.locator('#signal-pause')).to_have_attribute('aria-label', '播放回放')
        stopped = canvas.get_attribute('data-frame')
        page.wait_for_timeout(180)
        assert canvas.get_attribute('data-frame') == stopped
        page.locator('[data-metric="network"]').click()
        playwright.expect(page.locator('#signal-metric')).to_have_text('网络吞吐')
        playwright.expect(page.locator('#signal-value')).to_contain_text('KiB/s')
        position = page.locator('#signal-position')
        position.focus()
        page.keyboard.press('Home')
        playwright.expect(position).to_have_value('0')
        playwright.expect(page.locator('#signal-time')).to_contain_text('59.5 秒前')
        page.screenshot(path=str(tmp_path / f'website-replay-{width}.png'))
        page.locator('#signal-pause').click()
        playwright.expect(position).to_have_value('119')
        current_step = canvas.get_attribute('data-step')
        page.wait_for_function('(old) => document.querySelector("#signal-canvas").dataset.step !== old', arg=current_step)
        page.locator('#start-title').scroll_into_view_if_needed()
        page.wait_for_timeout(120)
        stopped = canvas.get_attribute('data-frame')
        page.wait_for_timeout(180)
        assert canvas.get_attribute('data-frame') == stopped
        page.locator('#signal-canvas').scroll_into_view_if_needed()
        page.wait_for_function('(old) => document.querySelector("#signal-canvas").dataset.frame !== old', arg=stopped)
        page.emulate_media(reduced_motion='reduce')
        playwright.expect(page.locator('#signal-pause')).to_have_attribute('aria-pressed', 'true')
        stopped = canvas.get_attribute('data-frame')
        page.wait_for_timeout(180)
        assert canvas.get_attribute('data-frame') == stopped
        assert not [url for url in requests if url.startswith(('http:', 'https:'))]
        assert not errors
        browser.close()


def test_resource_replay_respects_reduced_motion_without_javascript_layout_dependencies():
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(reduced_motion='reduce')
        page.goto(WEBSITE.as_uri())
        playwright.expect(page.locator('#demo-pause')).to_have_attribute('aria-pressed', 'true')
        cpu = page.frame_locator('#demo-panel').locator('#cpuValue')
        initial = cpu.inner_text()
        page.wait_for_timeout(200)
        assert cpu.inner_text() == initial
        page.locator('#signal-canvas').scroll_into_view_if_needed()
        playwright.expect(page.locator('#signal-pause')).to_have_attribute('aria-pressed', 'true')
        page.wait_for_timeout(150)
        frame = page.locator('#signal-canvas').get_attribute('data-frame')
        page.wait_for_timeout(180)
        assert page.locator('#signal-canvas').get_attribute('data-frame') == frame
        page.locator('[data-metric="memory"]').click()
        playwright.expect(page.locator('#signal-metric')).to_have_text('内存使用率')
        plain = browser.new_page(java_script_enabled=False, viewport={'width': 390, 'height': 844})
        plain.goto(WEBSITE.as_uri())
        playwright.expect(plain.locator('h1')).to_be_visible()
        playwright.expect(plain.locator('#install-code')).to_contain_text('github.com')
        assert plain.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_hero_resources_are_live_coherent_and_pause_offscreen(tmp_path, width):
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 960})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url))
        page.goto(WEBSITE.as_uri())
        demo = page.frame_locator('#demo-panel')
        cards = demo.locator('.server-status-grid')
        initial = cards.inner_text()
        traffic = float(demo.locator('#networkRxTotal').inner_text().split()[0])
        cpu = demo.locator('#cpuValue')
        playwright.expect(cpu).not_to_have_text(cpu.inner_text())
        assert cards.inner_text() != initial
        clock = demo.locator('#serverView')
        first = clock.get_attribute('data-frame')
        tiles = demo.locator('.load-cell').evaluate_all('(nodes) => nodes.map(n => n.getAttribute("style"))')
        playwright.expect(clock).not_to_have_attribute('data-frame', first)
        assert demo.locator('.load-cell').evaluate_all('(nodes) => nodes.map(n => n.getAttribute("style"))') != tiles
        page.locator('#demo-pause').click()
        values = cards.inner_text()
        stopped = clock.get_attribute('data-frame')
        page.wait_for_timeout(220)
        assert cards.inner_text() == values
        assert clock.get_attribute('data-frame') == stopped
        assert float(demo.locator('#networkRxTotal').inner_text().split()[0]) > traffic
        used, free = map(float, re.findall(r'[\d.]+', demo.locator('#memorySub').inner_text()))
        assert abs(used + free - 8) < .011
        assert abs(used / 8 * 100 - float(demo.locator('#memoryValue').inner_text().strip('%'))) < .12
        rx, tx = map(float, re.findall(r'[\d.]+', demo.locator('#networkSub').inner_text()))
        assert abs(float(demo.locator('#networkValue').inner_text().split()[0]) - rx - tx) < .01
        for resource in ['cpu', 'memory', 'disk', 'network']:
            meter = demo.locator(f'#{resource}Bar')
            fill = meter.locator('.resource-bar').evaluate_all('(nodes) => nodes.reduce((sum,n) => sum + Number(n.style.getPropertyValue("--fill")), 0)')
            assert abs(fill / 60 * 100 - float(meter.get_attribute('aria-valuenow'))) < .06
        page.screenshot(path=str(tmp_path / f'website-live-hero-{width}.png'))
        page.locator('#demo-pause').click()
        playwright.expect(clock).not_to_have_attribute('data-frame', stopped)
        page.locator('#start-title').scroll_into_view_if_needed()
        page.wait_for_timeout(150)
        hidden = cards.inner_text()
        stopped = clock.get_attribute('data-frame')
        page.wait_for_timeout(220)
        assert cards.inner_text() == hidden
        assert clock.get_attribute('data-frame') == stopped
        page.locator('#demo-dashboard').scroll_into_view_if_needed()
        playwright.expect(clock).not_to_have_attribute('data-frame', stopped)
        page.emulate_media(reduced_motion='reduce')
        playwright.expect(page.locator('#demo-pause')).to_have_attribute('aria-pressed', 'true')
        reduced = cards.inner_text()
        page.wait_for_timeout(220)
        assert cards.inner_text() == reduced
        demo.locator('#cpuCoreToggle').click()
        playwright.expect(demo.locator('#cpuCorePanel')).to_be_visible()
        playwright.expect(demo.locator('.cpu-core-cell')).to_have_count(8)
        demo.locator('#cpuCoreToggle').click()
        demo.locator('[data-observation-metric="memory_percent"]').click()
        playwright.expect(demo.locator('#loadAverageLabel')).to_have_text('内存 平均占用')
        demo.get_by_role('combobox', name='观测对象').click()
        demo.get_by_role('option', name='demo-api 容器').click()
        demo.locator('[data-trend-range="24h"]').click()
        playwright.expect(demo.locator('#systemTrendHint')).to_contain_text('24h')
        demo.locator('#observationLocate').click()
        playwright.expect(demo.locator('.load-cell[data-pinned="true"]')).to_have_count(1)
        page.locator('#demo-panel').screenshot(path=str(tmp_path / f'website-native-timeline-{width}.png'))
        demo.locator('#loadHistoryOpen').click()
        playwright.expect(demo.locator('#demo-history')).to_be_visible()
        playwright.expect(demo.locator('#demo-history-detail')).to_contain_text('未连接服务器')
        demo.locator('#demo-history-close').click()
        assert not [url for url in requests if url.startswith(('http:', 'https:'))]
        assert not errors
        browser.close()
