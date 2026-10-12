"""Durability, bounded retrieval and credential/target boundaries for chat history."""
import json
import os

import pytest

import agent
import terminal_ai as ai
import terminal_history as history
import test_terminal_ai as ai_tests
from test_ssh_terminal import request

manager = ai_tests.manager
model_server = ai_tests.model_server
http_server = ai_tests.http_server
runtime = ai_tests.runtime


def test_saved_chat_survives_restart_logout_and_new_login_without_restoring_grants(manager, model_server):
    manager.save(ai_tests.config(model_server))
    manager.set_access('old-login', 'ssh', {'mode': 'auto', 'consent': True})
    model_server.content = {'choices': [{'message': {'content': 'DNS failed\npassword=answer-secret\ntest-key-only'}}]}
    first = manager.submit('old-login', action='chat', target='demo@host:22', session_id='ssh',
                           output='password=terminal-secret', data=ai_tests.chat_data(manager, '查看状态 password=question-secret'))
    assert ai_tests.finished(manager, 'old-login', first['id'])['status'] == 'done'
    manager.revoke('old-login')
    manager.shutdown()
    restored = ai.Manager(manager.directory)
    try:
        saved = restored.history.detail(first['history_id'])
        serialized = json.dumps(saved)
        for secret in ('answer-secret', 'terminal-secret', 'question-secret', 'test-key-only'):
            assert secret not in serialized
        assert len(saved['turns']) == 1 and 'DNS failed' in saved['turns'][0]['answer']
        assert restored.access('new-login', 'new-ssh')['mode'] == 'approve' and not restored.permissions
        with pytest.raises(history.HistoryError, match='对应的服务器'):
            restored.submit('new-login', action='chat', session_id='other', target='demo@other:22',
                            data=ai_tests.chat_data(restored, history_id=first['history_id']))
        model_server.content = None
        followup = restored.submit('new-login', action='chat', session_id='new-ssh', target='demo@host:22',
                                   data=ai_tests.chat_data(restored, '下一步呢', history_id=first['history_id']))
        assert ai_tests.finished(restored, 'new-login', followup['id'])['status'] == 'done'
        assert followup['history_id'] == first['history_id']
        assert 'DNS failed' in json.dumps(model_server.calls[-1][2])
        assert len(restored.history.detail(first['history_id'])['turns']) == 2
        assert 'test-key-only' not in restored.history.path.read_bytes().decode('latin1')
    finally:
        restored.shutdown()


def test_history_paginates_full_turns_without_expiring_or_deleting_old_records(tmp_path):
    store = history.Store(tmp_path / 'ai')
    for i in range(60):
        store.begin('chat', f'job-{i}', f'question-{i}', '', 'model', 'context' * 4000)
        store.finish(f'job-{i}', 'answer' * 8000, 'done', [])
    page = store.detail('chat')
    assert len(page['turns']) == 50 and page['turns'][0]['question'] == 'question-10'
    assert len(page['turns'][-1]['answer']) == 48000
    older = store.detail('chat', page['next_before'])
    assert len(older['turns']) == 10 and older['next_before'] is None
    assert set(t['id'] for t in older['turns']).isdisjoint(t['id'] for t in page['turns'])
    context = store.context('chat', '')
    assert len(context) == 12 and context[-1]['content'] == 'answer' * 8000
    assert context[-2]['content'] == 'context' * 4000
    for i in range(45):
        store.begin(f'other-{i}', f'other-job-{i}', str(i), '', 'model', 'question')
    recent = store.list()
    assert len(recent['conversations']) == 40 and recent['next_offset'] == 40
    assert len(store.list(40)['conversations']) == 6
    assert len(store.detail('chat')['turns']) == 50
    if os.name != 'nt':
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.directory.stat().st_mode & 0o777 == 0o700


def test_history_unfinished_question_is_durable_and_corrupt_database_is_preserved(tmp_path):
    store = history.Store(tmp_path / 'ai')
    store.begin('chat', 'job', '待处理问题', '', 'model', 'question')
    assert history.Store(store.directory).detail('chat')['turns'][0]['status'] == 'running'
    store.path.write_bytes(b'broken database')
    with pytest.raises(history.HistoryError, match='原记录已保留'):
        store.list()
    assert store.path.read_bytes() == b'broken database'


def test_history_missing_file_is_not_created_by_listing(tmp_path):
    store = history.Store(tmp_path / 'ai')
    assert store.list()['conversations'] == [] and not store.path.exists()


def test_general_history_binds_to_server_when_first_used_for_ssh_inspection(tmp_path):
    store = history.Store(tmp_path / 'ai')
    store.begin('chat', 'general', '普通提问', '', 'model', 'general context')
    store.finish('general', '普通回复', 'done', [])
    assert store.context('chat', 'root@first:22')
    store.begin('chat', 'inspection', '检查服务器', 'root@first:22', 'model', 'server context')
    store.finish('inspection', '检查结果', 'done', [])
    assert store.detail('chat')['conversation']['target'] == 'root@first:22'
    with pytest.raises(history.HistoryError, match='对应的服务器'):
        store.context('chat', 'root@other:22')
    with pytest.raises(history.HistoryError, match='对应的服务器'):
        store.begin('chat', 'wrong-server', '检查', 'root@other:22', 'model', 'unsafe context')
    assert len(store.detail('chat')['turns']) == 2


def test_history_write_failure_is_visible_without_discarding_model_reply(manager, model_server, monkeypatch):
    manager.save(ai_tests.config(model_server))
    def fail(*args):
        raise history.HistoryError('历史保存失败')
    monkeypatch.setattr(manager.history, 'finish', fail)
    job = manager.submit('owner', action='chat', data=ai_tests.chat_data(manager, '查看状态'))
    result = ai_tests.finished(manager, 'owner', job['id'])
    assert result['status'] == 'done' and result['text']
    assert result['history_error'] == '历史保存失败'


def test_history_routes_require_login_and_transport_and_restore_for_new_admin_login(http_server, manager, model_server, monkeypatch):
    manager.save(ai_tests.config(model_server))
    first = manager.submit('owner', action='chat', data=ai_tests.chat_data(manager, '历史问题'))
    ai_tests.finished(manager, 'owner', first['id'])
    assert request(http_server, 'GET', '/terminal-ai/history', authenticated=False)[0] == 401
    status, result = request(http_server, 'GET', '/terminal-ai/history')
    assert status == 200 and result['conversations'][0]['id'] == first['history_id']
    status, result = request(http_server, 'GET', '/terminal-ai/history?id=' + first['history_id'])
    assert status == 200 and result['turns'][0]['question'] == '历史问题'
    for query in ('?offset=-1', '?before=0', '?offset=999999999999999999999', '?before=999999999999999999999'):
        assert request(http_server, 'GET', '/terminal-ai/history' + query)[0] == 400
    monkeypatch.setattr(agent.Handler, '_ssh_secure', lambda self: False)
    assert request(http_server, 'GET', '/terminal-ai/history')[0] == 403


def test_expired_live_chat_can_resume_from_saved_history(manager, model_server):
    manager.save(ai_tests.config(model_server))
    first = manager.submit('owner', action='chat', data=ai_tests.chat_data(manager, '第一次提问'))
    ai_tests.finished(manager, 'owner', first['id'])
    manager.conversations[first['conversation_id']]['updated'] -= 1801
    followup = manager.submit('owner', action='chat', data=ai_tests.chat_data(
        manager, '继续', conversation_id=first['conversation_id'], history_id=first['history_id']))
    assert ai_tests.finished(manager, 'owner', followup['id'])['status'] == 'done'
    assert followup['conversation_id'] != first['conversation_id']
    assert followup['history_id'] == first['history_id']
    assert '第一次提问' in json.dumps(model_server.calls[-1][2], ensure_ascii=False)
