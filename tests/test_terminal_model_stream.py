"""Real HTTP streams, slow headers, cancellation and uncut model answers."""
import json
import threading
import time
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import terminal_ai as ai
from test_terminal_ai import manager as manager, config, chat_data


@pytest.fixture
def stream_server(request, tmp_path, monkeypatch):
    state = SimpleNamespace(calls=[], packets=[], gate=None, delay=0, entered=threading.Event(),
                            sent=threading.Event(), finish=threading.Event(), json=None)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state.calls.append(body)
            state.entered.set()
            if state.delay:
                state.finish.wait(state.delay)
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json' if state.json else 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                if state.json:
                    self.wfile.write(json.dumps(state.json).encode())
                    return
                for packet in state.packets:
                    encoded = (packet if isinstance(packet, str) else json.dumps(packet, ensure_ascii=False)).encode()
                    self.wfile.write(b'data: ' + encoded + b'\n\n')
                    self.wfile.flush()
                state.sent.set()
                if state.gate:
                    while not state.gate.wait(.05) and not state.finish.is_set():
                        pass
            except (OSError, ConnectionError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    tls = getattr(request, 'param', False)
    if tls:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(minutes=10))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
            .sign(key, hashes.SHA256()))
        cert_path, key_path = tmp_path / 'test-ca.pem', tmp_path / 'test-key.pem'
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        client_context = ssl.create_default_context(cafile=str(cert_path))
        monkeypatch.setattr(ssl, '_create_default_https_context', lambda **_: client_context)
    state.origin = f'{"https" if tls else "http"}://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    state.finish.set()
    if state.gate:
        state.gate.set()
    server.shutdown()
    server.server_close()
    thread.join(3)


def wait(manager, key, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = manager.job('owner', key)
        if result['status'] in {'done', 'failed', 'cancelled'}:
            return result
        time.sleep(.02)
    pytest.fail('model task did not finish')


def start(manager, server, protocol='openai', **settings):
    manager.save({**config(server, protocol), **settings})
    return manager.submit('owner', action='chat', data=chat_data(manager))


def delta(text='', **fields):
    return {'choices': [{'index': 0, 'delta': {'content': text, **fields}}]}


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_native_stream_preserves_text_tools_usage_and_configurable_budget(stream_server, protocol):
    if protocol == 'openai':
        stream_server.packets = [delta('第一行\n'), delta('第二行'),
            delta(tool_calls=[{'index': 0, 'id': 'call_1', 'function': {'name': 'read_text_file', 'arguments': '{"pa'}}]),
            delta(tool_calls=[{'index': 0, 'function': {'arguments': 'th":"/log"}'}}]),
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]},
            {'choices': [], 'usage': {'total_tokens': 12}}, '[DONE]']
    else:
        stream_server.packets = [
            {'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': '第一行\n第二行'}},
            {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'call_1', 'name': 'read_text_file', 'input': {}}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"path":'}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '"/log"}'}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 2}},
            {'type': 'message_stop'}]
    updates = []
    result = ai.complete({**config(stream_server, protocol), 'max_tokens': 32768}, [{'role': 'user', 'content': '检查'}],
                         tools=[{'type': 'function', 'function': {'name': 'read_text_file', 'description': 'read', 'parameters': {}}}], publish=updates.append)
    assert result['text'] == '第一行\n第二行'
    assert json.loads(result['calls'][0]['function']['arguments']) == {'path': '/log'}
    assert stream_server.calls[0]['stream'] is True and stream_server.calls[0]['max_tokens'] == 32768
    assert updates


def test_slow_generation_beyond_thirty_seconds_succeeds(manager, stream_server):
    stream_server.delay = 31
    stream_server.packets = [delta('慢模型仍然完成\n'), {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}, '[DONE]']
    submitted = start(manager, stream_server)
    result = wait(manager, submitted['id'], 36)
    assert result['status'] == 'done' and '慢模型仍然完成' in result['text']
    assert 'max_tokens' not in stream_server.calls[0]


@pytest.mark.parametrize('before_headers', [True, False])
def test_cancel_frees_worker_during_headers_or_stream(manager, stream_server, before_headers):
    stream_server.delay = 10 if before_headers else 0
    stream_server.packets = [delta('可见的第一行\n')]
    stream_server.gate = threading.Event()
    submitted = start(manager, stream_server)
    assert stream_server.entered.wait(2)
    if not before_headers:
        assert stream_server.sent.wait(2)
        deadline = time.monotonic() + 2
        while not manager.job('owner', submitted['id']).get('partial_text') and time.monotonic() < deadline:
            time.sleep(.02)
        assert manager.job('owner', submitted['id'])['partial_text'] == '可见的第一行'
    manager.cancel('owner', submitted['id'])
    assert wait(manager, submitted['id'], 2)['status'] == 'cancelled'
    assert manager.jobs[submitted['id']]['_active'] is False


def test_large_answer_saved_and_restored_without_byte_or_character_truncation(manager, stream_server):
    text = '长回复中文\n' * 16000
    stream_server.json = {'choices': [{'message': {'content': text}}]}
    submitted = start(manager, stream_server)
    result = wait(manager, submitted['id'])
    assert result['status'] == 'done' and result['text'] == text.strip()
    archived = manager.history.detail(submitted['history_id'])
    assert archived['turns'][0]['answer'] == text.strip()
    assert manager.history.context(submitted['history_id'], '')[-1]['content'] == text.strip()


def test_incomplete_stream_does_not_execute_tools(manager, stream_server):
    stream_server.packets = [delta('正在分析\n'), delta(tool_calls=[{'index': 0, 'id': 'call_bad',
        'function': {'name': 'restart_service', 'arguments': '{'}}])]
    submitted = start(manager, stream_server)
    result = wait(manager, submitted['id'])
    assert result['status'] == 'failed' and '意外断开' in result['detail']
    assert not result['events']
    assert '正在分析' in manager.history.detail(submitted['history_id'])['turns'][0]['answer']


def test_optional_inactivity_timeout_and_invalid_settings(manager, stream_server):
    stream_server.delay = 3
    submitted = start(manager, stream_server, response_timeout=1)
    assert wait(manager, submitted['id'])['status'] == 'failed'
    for data in ({'response_timeout': -1}, {'max_tokens': 0}, {'max_tokens': True}):
        with pytest.raises(ai.AIError):
            manager.save({**config(stream_server), **data})
    assert manager.settings()['response_timeout'] == 1


@pytest.mark.parametrize('stream_server', [True], indirect=True)
def test_https_uses_certificate_validation_and_can_cancel_stream(manager, stream_server):
    stream_server.packets = [delta('安全 TLS 流\n')]
    stream_server.gate = threading.Event()
    submitted = start(manager, stream_server)
    assert stream_server.sent.wait(3)
    deadline = time.monotonic() + 2
    while not manager.job('owner', submitted['id']).get('partial_text') and time.monotonic() < deadline:
        time.sleep(.02)
    assert 'TLS' in manager.job('owner', submitted['id'])['partial_text']
    manager.cancel('owner', submitted['id'])
    assert wait(manager, submitted['id'], 2)['status'] == 'cancelled'
