"""Browser settings and partial Markdown output through actual model streams."""
import threading

from test_ssh_browser import page as page, playwright
from test_ssh_terminal import http_server as http_server, runtime as runtime
from test_terminal_ai import manager as manager
from test_terminal_model_stream import stream_server as stream_server, config, delta


def test_partial_reply_stays_visible_and_stop_finishes_without_waiting(page, http_server, manager, stream_server, tmp_path):
    manager.save(config(stream_server))
    stream_server.packets = [delta('## 流式分析\n\n**CPU 正常**\n')]
    stream_server.gate = threading.Event()
    page.goto(http_server.origin + '/ui?view=terminal')
    page.locator('#aiChatOpen').click()
    page.locator('#aiQuestion').fill('检查一下')
    page.locator('#aiAskSend').click()
    playwright.expect(page.locator('.tai-answer h2')).to_have_text('流式分析')
    playwright.expect(page.locator('.tai-answer strong')).to_have_text('CPU 正常')
    playwright.expect(page.locator('#aiStop')).to_be_visible()
    assert next(iter(manager.jobs.values()))['_active']
    page.screenshot(path=str(tmp_path / 'streaming-desktop.png'))
    page.locator('#aiStop').click()
    playwright.expect(page.locator('#aiStop')).not_to_be_visible()
    page.get_by_role('button', name='模型设置', exact=True).click()
    page.locator('.tai-model-options summary').click()
    playwright.expect(page.locator('#aiResponseTimeout')).to_have_value('0')
    page.locator('#aiMaxTokens').fill('32768')
    page.locator('#aiResponseTimeout').fill('180')
    page.locator('#aiSave').click()
    playwright.expect(page.locator('#aiSettingsDialog')).not_to_be_visible()
    assert manager.settings()['max_tokens'] == 32768 and manager.settings()['response_timeout'] == 180
    page.get_by_role('button', name='模型设置', exact=True).click()
    page.set_viewport_size({'width': 390, 'height': 844})
    playwright.expect(page.locator('#aiMaxTokens')).to_have_value('32768')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'response-settings-mobile.png'))
