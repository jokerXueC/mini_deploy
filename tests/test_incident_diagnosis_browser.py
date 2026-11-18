"""Automatic report controls, full Markdown and delivery receipts on desktop and phone."""
from copy import deepcopy
import re
import time

import pytest

from test_monitoring_browser import dashboard, playwright


@pytest.mark.parametrize('width,theme', [(1440,'dark'), (1440,'light'), (390,'dark'), (390,'light'), (320,'light')])
def test_report_enable_history_receipts_and_mobile_layout(dashboard, tmp_path, width, theme):
    page, state, origin = dashboard
    page.set_viewport_size({'width':width, 'height':960})
    key, now = 'a'*24, time.time()
    payload = {'enabled':False,'model_configured':True,'notifications_enabled':True,'target':'面板主机：example / abcdef',
        'items':[{'id':key,'title':'API 持续出现 502','updated':now,'state':'done','revision':2,'detail':'',
                  'delivery':[{'channel':'wecom','total':4,'sent':2,'failed':1}]}],
        'page':0, 'total':1,'has_more':False}
    text = '# API 异常诊断报告\n\n## 异常概况与影响\n\n接口出现 502。\n\n|证据|判断|\n|---|---|\n|代理日志|上游连接失败|\n\n## 解决方案\n\n```sh\ndocker ps\n```\n\n**建议尚未执行。**\n\n<script>window.reportXss=1</script><img src=x onerror="window.reportXss=1">'
    report = {'incident':{'title':'API 持续出现 502'}, 'state':'done',
        'versions':[{'revision':2,'created':now},{'revision':1,'created':now-300}],
        'report':{'body':text,'revision':2,'complete':True,'confidence':'suspected',
                  'trace':[{'title':'读取代理日志','status':'done','result':'ERROR upstream connection refused','command':'read_logs'}]},
        'delivery':[{'channel':'wecom','part':1,'total':4,'status':'sent','attempts':1,'attempt_at':now,'detail':'平台已接收'},
                    {'channel':'wecom','part':3,'total':4,'status':'failed','attempts':2,'attempt_at':now,'detail':'网络异常，自动重试'}]}
    posts = []
    def api(route):
        if route.request.method == 'POST':
            data = route.request.post_data_json
            assert route.request.headers['x-csrf-token'] == 'test-token'
            posts.append(data)
            if data['action'] == 'configure': payload['enabled'] = data['enabled']
            else: report['state'] = 'queued'
            route.fulfill(json=payload)
        elif '/report?' in route.request.url:
            if 'download=md' in route.request.url:
                route.fulfill(body=text, content_type='text/markdown', headers={'Content-Disposition':'attachment; filename=report.md'})
            else:
                value = deepcopy(report)
                if 'revision=1' in route.request.url: value['report'].update(revision=1, body='# 第一版完整报告')
                route.fulfill(json=value)
        else: route.fulfill(json=payload)
    page.route(re.compile(r'/incident-diagnosis(?:[/?].*)?$'), api)
    page.goto(origin+'/ui?view=events')
    page.evaluate('theme => document.documentElement.dataset.theme = theme', theme)
    playwright.expect(page.locator('#diagnosisReadiness')).to_contain_text('已关闭')
    page.locator('#diagnosisEnabled').check()
    playwright.expect(page.locator('#diagnosisReadiness')).to_contain_text('已开启')
    page.locator('[data-diagnosis]').click()
    playwright.expect(page.locator('#diagnosisDialog')).to_be_visible()
    playwright.expect(page.locator('#diagnosisReportBody table')).to_be_visible()
    playwright.expect(page.locator('#diagnosisReportBody strong')).to_have_text('建议尚未执行。')
    playwright.expect(page.locator('#diagnosisVersion')).to_have_class('form-select-native')
    button = page.locator('#diagnosisVersion-button')
    button.click()
    menu = page.locator('#diagnosisVersion-menu')
    playwright.expect(menu).to_be_visible()
    assert menu.evaluate('(e) => e.closest("dialog").id') == 'diagnosisDialog'
    bounds = menu.bounding_box()
    assert bounds['x'] >= 11 and bounds['x'] + bounds['width'] <= width - 11
    # A background refresh must not dismiss a menu being used.
    page.evaluate('IncidentDiagnosis.refresh()')
    playwright.expect(menu).to_be_visible()
    page.keyboard.press('Escape')
    playwright.expect(menu).not_to_be_visible()
    playwright.expect(page.locator('#diagnosisDialog')).to_be_visible()
    code = page.locator('#diagnosisReportBody pre code')
    assert 'Consolas' in code.evaluate('(e) => getComputedStyle(e).fontFamily')
    assert code.evaluate('(e) => getComputedStyle(e).backgroundColor') == 'rgba(0, 0, 0, 0)'
    assert page.locator('#diagnosisReportBody pre').evaluate('(e) => getComputedStyle(e).borderTopWidth') == '1px'
    assert not page.locator('#diagnosisReportBody script, #diagnosisReportBody img').count()
    assert page.evaluate('window.reportXss') is None
    page.locator('#diagnosisEvidence summary').first.click()
    page.locator('#diagnosisTrace summary').click()
    playwright.expect(page.locator('#diagnosisTrace pre')).to_contain_text('upstream connection refused')
    page.locator('#diagnosisReceipts summary').click()
    playwright.expect(page.locator('#diagnosisDelivery')).to_contain_text('第 3/4 条')
    with page.expect_download() as download:
        page.locator('#diagnosisDownload').click()
    assert download.value.suggested_filename == 'report.md'
    page.screenshot(path=str(tmp_path / f'diagnosis-report-{width}-{theme}.png'))
    assert page.locator('#diagnosisDialog').evaluate('(e) => e.scrollWidth <= e.clientWidth')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    button.click()
    menu.get_by_role('option').filter(has_text='v1 ·').click()
    playwright.expect(page.locator('#diagnosisReportBody')).to_contain_text('第一版完整报告')
    page.locator('#diagnosisRetry').click()
    playwright.expect(page.locator('#diagnosisReportStatus')).to_contain_text('等待诊断')
    page.locator('#diagnosisClose').click()
    playwright.expect(page.locator('#diagnosisDialog')).not_to_be_visible()
    assert posts == [{'action':'configure','enabled':True}, {'action':'retry','id':key}]
    assert not state['errors']
