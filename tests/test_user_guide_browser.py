"""The bundled guide remains usable on HTTP, mobile and missing API responses."""
import re

import pytest

import agent
import setup_checks
from test_onboarding_browser import browser_page as browser_page, playwright, ROOT


def guide_api(page):
    installation = setup_checks.installation_commands('/srv/custom-panel', '/srv/custom-data',
        '/etc/custom-panel.env', 'custom-panel-agent', '/opt/python/bin/python3')
    page.route('**/setup/checks', lambda route: route.fulfill(json={'installation': installation}))
    return installation


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_guide_topics_search_copy_download_and_layout(browser_page, tmp_path, width, theme):
    page, state = browser_page
    installation = guide_api(page)
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    page.locator('#userGuideOpen').click()
    playwright.expect(page.locator('#userGuideNav a')).to_have_count(19)
    page.locator('[data-guide-link=local]').click()
    playwright.expect(page.locator('[data-guide-topic=local]')).to_contain_text('/srv/custom-panel')
    # Simulate HTTP/denied clipboard access; fallback must live inside the modal.
    page.evaluate('''() => {
      Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: async () => {throw Error('denied');}}});
      document.execCommand = () => {window.guideCopied = document.activeElement.value; return true;};
    }''')
    page.locator('[data-guide-topic=local] .guide-copy').first.click()
    assert page.evaluate('window.guideCopied') == installation['commands'][0]['command']
    playwright.expect(page.locator('#userGuideFeedback')).to_contain_text('已复制')
    page.locator('#userGuideSearch').fill('nftables')
    playwright.expect(page.locator('[data-guide-topic=security]')).to_be_visible()
    playwright.expect(page.locator('[data-guide-topic=install]')).to_be_hidden()
    page.locator('#userGuideSearch').fill('definitely-no-matching-guide-topic')
    playwright.expect(page.locator('#userGuideEmpty')).to_be_visible()
    page.locator('#userGuideSearch').fill('')
    page.locator('[data-guide-link=requests]').click()
    playwright.expect(page.locator('[data-guide-link=requests]')).to_have_attribute('aria-current', 'location')
    page.screenshot(path=str(tmp_path / f'guide-{width}-{theme}.png'))
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('#userGuideDialog').evaluate('(el) => el.scrollWidth <= el.clientWidth')
    assert page.locator('#userGuideContent').evaluate('(el) => el.scrollWidth <= el.clientWidth')
    with page.expect_download() as download:
        page.get_by_role('link', name='下载指南文档').click()
    downloaded = download.value.path()
    assert downloaded.read_bytes() == (ROOT / 'ui/user-guide.md').read_bytes()
    assert not state['posts']
    page.keyboard.press('Escape')
    playwright.expect(page.locator('#userGuideDialog')).not_to_be_visible()
    playwright.expect(page.locator('#userGuideOpen')).to_be_focused()


def test_feature_help_opens_specific_topic_and_survives_missing_installation_api(browser_page):
    page, _ = browser_page
    page.route('**/setup/checks', lambda route: route.fulfill(status=503, json={'detail': 'offline'}))
    page.evaluate('''() => {
      const button = document.createElement('button'); button.dataset.userGuide = 'ssh'; button.id = 'testFeatureHelp';
      document.body.append(button);
    }''')
    page.locator('#testFeatureHelp').click()
    playwright.expect(page.locator('[data-guide-link=ssh]')).to_have_attribute('aria-current', 'location')
    playwright.expect(page.locator('#userGuideFeedback')).to_contain_text('本机安装信息未取得')
    playwright.expect(page.locator('[data-guide-topic=ssh]')).to_contain_text('python3-paramiko')


def test_installation_success_link_opens_guide_after_navigation(browser_page):
    page, _ = browser_page
    guide_api(page)
    page.route('**/ui?view=setup&guide=install', lambda route: route.fulfill(
        path=str(ROOT / 'ui/index.html'), content_type='text/html'))
    page.goto(page.url + '?view=setup&guide=install')
    playwright.expect(page.locator('#userGuideDialog')).to_be_visible()
    playwright.expect(page.locator('[data-guide-link=install]')).to_have_attribute('aria-current', 'location')
    playwright.expect(page.locator('[data-guide-topic=install]')).to_contain_text('mktemp')


def test_guide_download_failure_can_retry_and_markdown_is_sanitized(browser_page):
    page, _ = browser_page
    guide_api(page)
    page.route('**/ui/user-guide.md', lambda route: route.fulfill(status=503, body='unavailable'))
    page.locator('#userGuideOpen').click()
    playwright.expect(page.locator('#userGuideFeedback')).to_contain_text('加载未完成')
    page.unroute('**/ui/user-guide.md')
    page.route('**/ui/user-guide.md', lambda route: route.fulfill(body='''<h2 id="start">Guide</h2>

<img src=x onerror="window.guideInjected=1"><script>window.guideInjected=1</script>

[bad](javascript:alert(1))

```bash
printf '%s\\n' '<script>literal</script>'
```
''', content_type='text/markdown'))
    page.locator('#userGuideRetry').click()
    playwright.expect(page.locator('[data-guide-topic=start]')).to_contain_text('<script>literal</script>')
    assert page.evaluate('window.guideInjected === undefined')
    assert not page.locator('#userGuideContent script, #userGuideContent a[href^="javascript:"]').count()


def test_guide_assets_and_all_feature_links_ship_in_existing_ui_entry():
    guide = (ROOT / 'ui/user-guide.md').read_text(encoding='utf-8')
    topics = re.findall(r'<h2 id="([a-z-]+)">', guide)
    assert len(topics) == len(set(topics))
    assert {'login', 'resources', 'history', 'docker', 'requests', 'certificates', 'monitoring', 'ssh', 'ai', 'security', 'notifications'} <= set(topics)
    assert set(re.findall(r'\]\(#([a-z-]+)\)', guide)) <= set(topics)
    for name in ('user-guide.md', 'user-guide.js', 'user-guide.css'):
        assert name in agent.UI_ASSET_TYPES and (ROOT / 'ui' / name).is_file()
