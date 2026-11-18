"""Saved incident reports remain usable in global conversations after recovery."""
import json
import sqlite3
import threading

import pytest

import agent
import panel_diagnostics
import terminal_ai
import terminal_history
import terminal_tools
import test_terminal_ai as ai_tests
from test_incident_diagnosis import store as store, complete, KEY, result
from test_panel_assistant import backend, evidence as evidence, manager as manager, model_server as model_server
from test_deployment_removal import http_server as http_server, runtime as runtime
from test_ssh_terminal import request
from test_terminal_agent import answer


def read(assistant, **args):
    return json.loads(assistant.execute(assistant.plan('get_diagnosis_report',
        {'id': KEY, 'revision': 1, **args}), threading.Event())['output'])


def test_reference_pins_report_and_original_evidence_after_recovery(store, evidence):
    complete(store)
    reference = store.reference({'id': KEY, 'revision': 1})
    original = store.detail(KEY, 1)['report']['body']
    store.scan({'events': [{'kind': 'recovery', 'incident_id': KEY, 'at_ts': 1100,
                          'title': 'CPU 恢复', 'detail': 'CPU 20%'}]})
    assert store.detail(KEY)['report']['revision'] == 2
    assistant, _ = backend(evidence, diagnoses=store, reference=reference)
    initial = assistant.initial_call()['function']
    assert initial['name'] == 'get_diagnosis_report'
    assert json.loads(initial['arguments']) == {'id': KEY, 'revision': 1}
    data = read(assistant)
    assert data['content'] == original and '监测恢复更新' not in data['content']
    assert data['incident']['id'] == 'server:cpu'
    assert read(assistant, part='evidence')['content'].find('get_panel_incident') >= 0
    plan = assistant.plan('list_diagnosis_reports', {})
    listing = json.loads(assistant.execute(plan, threading.Event())['output'])
    assert listing['items'][0]['id'] == KEY and listing['items'][0]['revision'] == 2
    with pytest.raises(terminal_tools.ToolError):
        assistant.plan('ssh_restart_service', {'session_id': 'unconnected', 'service': 'nginx'})


def test_full_report_and_evidence_pagination_redacts_before_slicing(store, evidence):
    body = '故障证据\n' * 4200 + 'password=private-report\n最终结论'
    trace = [{'title': '原始日志', 'status': 'done', 'password': 'private-field',
              'result': 'upstream failed\n' * 1400 + 'token=private-token\n现场结束'}]
    complete(store, status='failed', text=body, events=trace)
    assistant, _ = backend(evidence, diagnoses=store)
    for part, expected in [('report', terminal_ai.redact(store.detail(KEY, 1)['report']['body'], strip=False)),
                           ('evidence', json.dumps(panel_diagnostics.scrub(trace[0]), ensure_ascii=False, indent=2))]:
        offset, pieces = 0, []
        for _ in range(80):
            data = read(assistant, part=part, offset=offset, limit=4000)
            pieces.append(data['content'])
            assert not data['next_offset'] or data['next_offset'] > offset
            if data['next_offset'] is None:
                break
            offset = data['next_offset']
        else:
            pytest.fail('Report pagination did not terminate')
        assert ''.join(pieces) == expected
        assert data['total_characters'] == len(expected)
        assert 'private-' not in ''.join(pieces)
    for args in ({'revision': 99}, {'offset': 2**30}, {'part': 'evidence', 'event_index': 9}):
        with pytest.raises(terminal_tools.ToolError):
            read(assistant, **args)


@pytest.mark.parametrize('reference', [None, {}, {'id': KEY}, {'id': KEY, 'revision': True},
    {'id': KEY, 'revision': '1'}, {'id': KEY, 'revision': 0}, {'id': KEY, 'revision': 99},
    {'id': '../file', 'revision': 1}, {'id': [], 'revision': 1},
    {'id': KEY, 'revision': 1, 'body': 'untrusted client report'}])
def test_reference_rejects_malformed_missing_or_client_supplied_content(store, reference):
    complete(store)
    with pytest.raises(ValueError):
        store.reference(reference)


@pytest.mark.parametrize('args', [{'revision': True}, {'limit': 999}, {'offset': -1}, {'part': 'files'},
                                 {'event_index': '0'}, {'unexpected': 1}, {'id': 'alert-test'}])
def test_report_tool_rejects_invalid_arguments(store, evidence, args):
    assistant, _ = backend(evidence, diagnoses=store)
    with pytest.raises(terminal_tools.ToolError):
        assistant.plan('get_diagnosis_report', {'id': KEY, 'revision': 1, **args})


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_global_submission_reads_pinned_report_and_persists_reference_after_restart(
        http_server, manager, model_server, evidence, store, monkeypatch, protocol):
    complete(store)
    monkeypatch.setattr(agent, '_diagnosis_store', lambda: store)
    manager.save(ai_tests.config(model_server, protocol))
    model_server.content = answer(protocol)
    owner = terminal_ai.owner(http_server.cookie)
    reference = {'id': KEY, 'revision': 1}
    status, job = request(http_server, 'POST', '/terminal-ai/global',
        ai_tests.chat_data(manager, '结合报告核对当前状态', permission='approve', report_reference=reference))
    assert status == 200
    done = ai_tests.finished(manager, owner, job['id'])
    assert done['status'] == 'done'
    assert done['events'][0]['command'].startswith('get_diagnosis_report')
    payload = json.dumps(model_server.calls[-1][2], ensure_ascii=False)
    assert KEY in payload and 'CPU 持续高占用' in payload
    assert '原始工具结果保存在面板报告中' in payload
    saved = manager.history.detail(job['history_id'])
    assert saved['turns'][0]['reference'] == {**reference, 'title': 'CPU 持续高占用'}
    manager.shutdown()
    restored = terminal_ai.Manager(manager.directory)
    try:
        assistant, _ = backend(evidence, diagnoses=store, reference=store.reference(reference))
        followup = restored.submit('new-login', action='chat', target=done['target'], session_id='global-assistant',
            data=ai_tests.chat_data(restored, '下一步怎么验证', permission='approve', history_id=job['history_id']),
            executor=assistant.execute, evidence_backend=assistant)
        assert ai_tests.finished(restored, 'new-login', followup['id'])['status'] == 'done'
        assert followup['history_id'] == job['history_id']
        assert len(restored.history.detail(job['history_id'])['turns']) == 2
        assert KEY in json.dumps(model_server.calls[-1][2])
        assert not restored.permissions
    finally:
        restored.shutdown()


def test_reference_title_is_redacted_in_saved_chat(manager, model_server, store, evidence):
    complete(store)
    manager.save(ai_tests.config(model_server))
    model_server.content = answer('openai')
    reference = {**store.reference({'id': KEY, 'revision': 1}), 'title': 'test-key-only password=private-title'}
    assistant, _ = backend(evidence, diagnoses=store, reference=reference)
    job = manager.submit('owner', action='chat', session_id='global-assistant', target='panel',
        data=ai_tests.chat_data(manager, permission='approve'), executor=assistant.execute, evidence_backend=assistant)
    assert ai_tests.finished(manager, 'owner', job['id'])['status'] == 'done'
    saved = json.dumps(manager.history.detail(job['history_id']))
    assert 'private-title' not in saved and 'test-key-only' not in saved


def test_http_rejects_bad_reference_and_legacy_entry_before_model_call(
        http_server, manager, model_server, evidence, store, monkeypatch):
    complete(store)
    monkeypatch.setattr(agent, '_diagnosis_store', lambda: store)
    manager.save(ai_tests.config(model_server))
    for path, reference in [('/terminal-ai/global', {'id': KEY, 'revision': 99}),
                            ('/terminal-ai/global', {'id': KEY, 'revision': 1, 'title': 'forged'}),
                            ('/terminal-ai/panel', {'id': KEY, 'revision': 1})]:
        status, result = request(http_server, 'POST', path,
            ai_tests.chat_data(manager, permission='approve', report_reference=reference))
        assert status == 400 and result.get('detail')
    assert not model_server.calls


def test_existing_history_schema_migrates_without_losing_old_turns(tmp_path):
    store = terminal_history.Store(tmp_path / 'history')
    store.begin('chat', 'old-job', '旧问题', 'panel', 'model', '旧上下文')
    store.finish('old-job', '旧回答', 'done', [])
    with sqlite3.connect(store.path) as db:
        db.execute('ALTER TABLE turns DROP COLUMN reference')
    assert store.detail('chat')['turns'][0]['reference'] == {}
    store.begin('chat', 'new-job', '新问题', 'panel', 'model', '新上下文', reference={'id': KEY, 'revision': 1})
    assert [t['question'] for t in store.detail('chat')['turns']] == ['旧问题', '新问题']
    assert store.context('chat', 'panel')[0]['content'] == '旧上下文'
