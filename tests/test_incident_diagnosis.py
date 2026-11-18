"""Durable event diagnosis and lossless, independently acknowledged notification parts."""
import json
import threading
import time
from urllib.request import Request, urlopen
from types import SimpleNamespace

import pytest

import agent
import incident_diagnosis as diagnosis
import terminal_ai
from test_deployment_removal import runtime, http_server
from test_terminal_ai import manager, model_server, config, finished


KEY = 'a' * 24


def incident(**extra):
    return {'id': 'server:cpu', 'incident_id': KEY, 'title': 'CPU 持续高占用', 'source': 'server',
            'first_seen': 995, 'last_sample': 1000, 'detail': 'CPU 95%', **extra}


def result(**extra):
    data = {name: '证据及说明：' + title for name, title in diagnosis.SECTIONS}
    return {'status': 'done', 'text': json.dumps({**data, 'confidence': 'unknown'}, ensure_ascii=False),
            'events': [{'title': '读取故障现场', 'status': 'done', 'command': 'get_panel_incident', 'result': '{"cpu":95}'}], **extra}


@pytest.fixture
def store(tmp_path):
    now = [1000.0]
    store = diagnosis.Store(tmp_path, clock=lambda: now[0])
    store.now = now
    return store


def complete(store, **extra):
    store.configure(True)
    store.scan({'alerts': [incident()], 'events': []})
    store.running(KEY, 'job')
    store.finish(KEY, result(**extra), '测试主机')


def test_disabled_idle_and_episode_dedup_recovery_versions(store):
    store.scan({'alerts': [incident()]})
    assert not store.due()
    complete(store)
    for _ in range(3):
        store.scan({'alerts': [incident()], 'events': []})
    assert store.snapshot({})['total'] == 1
    assert not store.due()
    recovery = {'kind': 'recovery', 'incident_id': KEY, 'at_ts': 1100, 'title': 'CPU 恢复', 'detail': 'CPU 20%'}
    store.scan({'events': [recovery]})
    store.scan({'events': [recovery]})
    assert store.detail(KEY)['revision'] == 2
    assert '监测恢复更新' not in store.detail(KEY, 1)['report']['body']
    assert '不表示 AI 执行了修复' in store.detail(KEY)['report']['body']
    store.scan({'alerts': [incident(incident_id='b'*24)]})
    assert store.due()['id'] == 'b' * 24


@pytest.mark.parametrize('channel,budget', [('wecom',4096), ('dingtalk',18000), ('email',None)])
@pytest.mark.parametrize('body', ['中文🙂' * 20000, '开始\n\n```sql\n' + 'SELECT 1;\n' * 7000 + '```\n结束', 'x'*40000, '```'+'x'*2000+'\n'+'y'*10000], ids=['unicode','code','long-line','long-fence'])
def test_full_unicode_report_split_with_transport_overhead(channel, budget, body):
    parts = diagnosis.messages(body, channel, KEY, 123)
    assert ''.join(p['piece'] for p in parts) == body
    if budget:
        assert all(len(('### mini_deploy · 异常诊断报告\n\n'+p['body']).encode()) <= budget for p in parts)
    else:
        assert len(parts) == 1 and body in parts[0]['body']


def test_failed_part_blocks_next_and_restart_resumes_without_resending_success(store):
    complete(store, text=result()['text'].replace('异常概况与影响', '内容' * 6000))
    config = {'wecom': {'enabled': True, 'webhook_url': 'https://example.test/private-key'}}
    store.queue_notifications(config)
    first = store.next_delivery(config)
    assert first['part'] == 1
    store.receipt(first, True, 'ok')
    store.now[0] += 4
    second = store.next_delivery(config)
    assert second['part'] == 2
    store.receipt(second, False, 'retry')
    store.now[0] += 4
    assert store.next_delivery(config) is None
    store.now[0] += 70
    assert store.next_delivery(config)['part'] == 2
    store.restart()
    store.now[0] += 11
    retried = store.next_delivery(config)
    assert retried['part'] == 2
    store.receipt(retried, True, 'ok')
    for _ in range(30):
        store.now[0] += 4
        part = store.next_delivery(config)
        if not part:
            break
        assert part['part'] > 2
        store.receipt(part, True, 'ok')
    store.queue_notifications(config)
    assert store.next_delivery(config) is None
    with store.connect() as db:
        pieces = [r[0] for r in db.execute('SELECT piece FROM deliveries ORDER BY part')]
    assert ''.join(pieces) == store.detail(KEY)['report']['body']
    assert 'private-key' not in json.dumps(store.detail(KEY))
    changed = {'wecom': {'enabled': True, 'webhook_url': 'https://example.test/new'}}
    store.queue_notifications(changed)
    assert store.next_delivery(changed)['part'] == 1


def test_partial_response_kept_and_retries_bounded(store):
    complete(store, status='failed', text='未结束的原始分析\n' * 5000, detail='模型断开')
    detail = store.detail(KEY)
    assert not detail['report']['complete']
    assert ('未结束的原始分析\n' * 5000) in detail['report']['body']
    assert not store.due()
    for attempt in (2, 3):
        store.now[0] += 10000
        assert store.due()
        store.running(KEY, 'retry')
        store.finish(KEY, result(notice='模型截断'), 'host')
    store.now[0] += 10000
    assert not store.due()
    store.retry(KEY)
    assert store.due()['attempts'] == 0


@pytest.mark.parametrize('confidence', [[], {}, None, 3, True])
def test_malformed_model_confidence_preserves_raw_result_instead_of_blocking_queue(confidence):
    response = result(text=json.dumps({**{k:'evidence' for k,_ in diagnosis.SECTIONS}, 'confidence':confidence}))
    body, complete, confidence = diagnosis.render_report(incident(), response, 'host', 1000)
    assert complete is False and confidence == 'unknown'
    assert response['text'] in body


def test_report_storage_failure_never_marks_complete(store, monkeypatch):
    store.configure(True)
    store.scan({'alerts':[incident()]})
    store.running(KEY, 'job')
    def full(*args):
        raise OSError('disk full')
    monkeypatch.setattr(store, '_report', full)
    with pytest.raises(OSError):
        store.finish(KEY, result(), 'host')
    assert store.detail(KEY)['state'] == 'running'
    store.restart()
    assert store.due()['id'] == KEY


class Backend:
    def __init__(self, write=False):
        self.executed, self.write = [], write

    def schemas(self):
        return []

    def initial_call(self):
        return {'id':'initial', 'type':'function', 'function':{'name':'read_logs','arguments':'{}'}}

    def plan(self, name, args):
        return {'name':name, 'title':'读取故障日志', 'command':'read_logs', 'reason':'核对故障', 'write':self.write}

    def execute(self, plan, cancel):
        self.executed.append(plan['name'])
        return {'exit_code':0, 'output':'实际故障日志 password=redacted-secret', 'truncated':False}


def test_background_uses_readonly_tools_without_session_and_continues_long_report(manager, model_server):
    manager.save(config(model_server))
    text = result()['text']
    split = len(text) // 2
    model_server.content = lambda body, n: {'choices':[{'message':{'content':text[:split] if n == 1 else text[split:]}, 'finish_reason':'length' if n == 1 else 'stop'}]}
    backend = Backend()
    job = manager.submit_background(incident(), backend, 'host', diagnosis.GUIDANCE)
    output = finished(manager, diagnosis.OWNER, job['id'])
    assert output['status'] == 'done' and output['text'] == text and not output.get('notice')
    assert backend.executed == ['read_logs']
    assert 'redacted-secret' not in json.dumps(model_server.calls)
    assert output['events'][0]['started_at'] <= output['events'][0]['finished_at']
    with pytest.raises(terminal_ai.AIError):
        manager.job('browser-owner', job['id'])


def test_background_blocks_write_even_if_backend_proposes_it(manager, model_server):
    manager.save(config(model_server))
    model_server.content = {'choices':[{'message':{'content':result()['text']}}]}
    backend = Backend(write=True)
    output = finished(manager, diagnosis.OWNER, manager.submit_background(incident(), backend, 'host', diagnosis.GUIDANCE)['id'])
    assert not backend.executed
    assert output['events'][0]['status'] == 'blocked'


def test_worker_no_model_calls_when_idle_or_disabled_and_persists_report(store):
    calls, snapshot = [], {'alerts':[], 'events':[]}
    class Manager:
        def settings(self): return {'configured':True}
        def submit_background(self, *args): calls.append(args); return {'id':'job'}
        def job(self, *args): return result()
        def cancel(self, *args): calls.append('cancel')
        def release_background(self, *args): pass
    worker = diagnosis.Worker(store, monitor=lambda:snapshot, manager=Manager, backend=lambda i:Backend(),
        target=lambda:'host', config=lambda:{}, send=lambda *args:pytest.fail('unexpected notification'))
    try:
        worker.tick()
        store.configure(True)
        worker.tick()
        assert calls == []
        snapshot['alerts'] = [incident()]
        worker.tick()
        worker.tick()
        assert len(calls) == 1 and store.detail(KEY)['report']['complete']
        worker.tick()
        assert len(calls) == 1
        store.scan({'alerts':[incident(incident_id='b'*24)]})
        store.now[0] += 16
        worker.tick()
        store.configure(False)
        worker.tick()
        assert calls[-1] == 'cancel'
        assert store.detail('b'*24)['state'] == 'paused'
    finally:
        worker.pool.shutdown(wait=True)


def test_worker_retries_storage_initialization_instead_of_exiting(store, monkeypatch):
    attempts, stop = [], threading.Event()
    def restart():
        attempts.append(1)
        if len(attempts) == 1: raise OSError('temporarily busy')
        stop.set()
    monkeypatch.setattr(store, 'restart', restart)
    worker = diagnosis.Worker(store, monitor=lambda:{}, manager=lambda:None, backend=lambda i:None,
        target=lambda:'host', config=lambda:{}, send=lambda *args:None)
    worker.run(stop)
    assert len(attempts) == 2


def test_api_auth_configuration_and_full_report(http_server, runtime):
    from test_ssh_terminal import request
    assert http_server('GET', '/incident-diagnosis', authenticated=False)[0] == 401
    assert http_server('POST', '/incident-diagnosis', {'action':'configure','enabled':True}, authenticated=False)[0] == 401
    assert request(http_server, 'POST', '/incident-diagnosis', {'action':'configure','enabled':True}, csrf=False)[0] == 403
    assert http_server('GET', '/incident-diagnosis')[1]['enabled'] is False
    assert http_server('POST', '/incident-diagnosis', {'action':'configure','enabled':True})[1]['enabled']
    store = agent._diagnosis_store()
    store.scan({'alerts':[incident(last_sample=store.clock())]})
    store.running(KEY, 'job')
    store.finish(KEY, result(), 'host')
    status, payload = http_server('GET', '/incident-diagnosis/report?id='+KEY)
    assert status == 200 and payload['report']['complete']
    assert all(title in payload['report']['body'] for _, title in diagnosis.SECTIONS)
    download = Request(http_server.origin + '/incident-diagnosis/report?id='+KEY+'&download=md',
                       headers={'Cookie':f'{agent.COOKIE_NAME}={http_server.cookie}'})
    with urlopen(download) as response:
        assert response.read().decode() == payload['report']['body']
        assert 'attachment' in response.headers['Content-Disposition']
    assert http_server('GET', '/incident-diagnosis?page=-1')[0] == 400
    assert http_server('POST', '/incident-diagnosis', {'action':'configure','enabled':'true'})[0] == 400


def test_automatic_transport_exposes_full_host_read_tools_and_frozen_identity(monkeypatch):
    seen = []
    monkeypatch.setattr(agent, '_incident_diagnostics', lambda key, **kw: seen.append(kw['snapshot']) or {'incident':kw['snapshot']['alerts'][0]})
    monkeypatch.setattr(agent, '_monitor', lambda:SimpleNamespace(snapshot=lambda:{'alerts':[incident(incident_id='b'*24)],'events':[]}))
    backend = agent._automatic_diagnosis_backend(incident())
    names = {s['function']['name'] for s in backend.schemas()}
    assert {'read_logs','list_proxy_configs','read_host_file','get_request_records','get_read_capabilities'} <= names
    assert 'execute_command' not in names
    assert backend.incident('server:cpu')['incident']['incident_id'] == KEY
    assert seen[0]['alerts'][0]['incident_id'] == KEY


def test_model_configuration_failure_does_not_hide_persisted_reports(http_server, runtime, monkeypatch):
    store = agent._diagnosis_store()
    store.configure(True)
    store.scan({'alerts':[incident(last_sample=store.clock())]})
    store.running(KEY, 'job')
    store.finish(KEY, result(), 'host')
    class BrokenModel:
        def settings(self): raise terminal_ai.AIError('invalid model config')
    monkeypatch.setattr(agent, '_ai_manager', BrokenModel)
    status, payload = http_server('GET', '/incident-diagnosis')
    assert status == 200 and payload['items'][0]['revision'] == 1
    assert payload['model_configured'] is False and '模型配置暂不可读' in payload['error']


def test_completed_long_running_background_result_retained_until_durable_save(manager, model_server):
    manager.save(config(model_server))
    model_server.content = {'choices':[{'message':{'content':result()['text']}}]}
    job = manager.submit_background(incident(), Backend(), 'host', diagnosis.GUIDANCE)
    with manager.lock:
        manager.jobs[job['id']]['created'] = time.monotonic() - 3600
    assert finished(manager, diagnosis.OWNER, job['id'])['status'] == 'done'
    manager.release_background(job['id'])
    with pytest.raises(terminal_ai.AIError):
        manager.job(diagnosis.OWNER, job['id'])


def test_partial_prefix_preserved_when_continuation_fails(manager, model_server):
    manager.save(config(model_server))
    prefix = '{"symptom":"已读取的结果，但尚未完成'
    def response(body, n):
        if n > 1:
            return {'error':'provider failed'}
        return {'choices':[{'message':{'content':prefix}, 'finish_reason':'length'}]}
    model_server.content = response
    output = finished(manager, diagnosis.OWNER, manager.submit_background(incident(), Backend(), 'host', diagnosis.GUIDANCE)['id'])
    assert output['status'] == 'failed'
    assert prefix in output['partial_text']
    assert prefix in diagnosis.render_report(incident(), output, 'host', 1000)[0]


def test_recovery_during_run_and_unfinished_old_notification_version_preserved(store):
    complete(store, text=result()['text'].replace('异常概况与影响', '正文' * 5000))
    config = {'wecom':{'enabled':True}}
    store.queue_notifications(config)
    part = store.next_delivery(config)
    store.receipt(part, True, 'ok')
    store.scan({'events':[{'kind':'recovery','incident_id':KEY,'at_ts':1100,'title':'已恢复'}]})
    store.queue_notifications(config)
    with store.connect() as db:
        for revision in (1,2):
            body = ''.join(r[0] for r in db.execute('SELECT piece FROM deliveries WHERE revision=? ORDER BY part', (revision,)))
            assert body == store.detail(KEY, revision)['report']['body']


def test_worker_handles_missing_job_and_mock_channel_delivery(store):
    complete(store)
    sent, tasks = [], []
    class Manager:
        def settings(self): return {'configured':True}
        def submit_background(self, *args): tasks.append(1); return {'id':'missing'}
        def job(self, *args): raise terminal_ai.AIError('expired', 404)
        def release_background(self, *args): pass
    worker = diagnosis.Worker(store, monitor=lambda:{}, manager=Manager, backend=lambda i:Backend(),
        target=lambda:'host', config=lambda:{'email':{'enabled':True}}, send=lambda *args:sent.append(args) or 'accepted')
    try:
        worker.tick()
        worker.sending[0].result(timeout=2)
        # Receipt is committed on the next tick, independently from report generation.
        worker.tick()
        assert len(sent) == 1 and store.detail(KEY)['delivery'][0]['status'] == 'sent'
        assert store.detail(KEY)['report']['body'] in sent[0][-1]
        store.retry(KEY)
        worker.tick()
        worker.tick()
        assert len(tasks) == 1 and store.detail(KEY)['state'] == 'failed'
    finally:
        worker.pool.shutdown(wait=True)
