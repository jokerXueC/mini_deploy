"""Cancellable model HTTP requests and OpenAI/Claude event streams."""
from __future__ import annotations

import http.client
import io
import json
import socket
import time
from urllib.request import HTTPHandler, HTTPSHandler, HTTPRedirectHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class StreamError(ValueError):
    pass


def receive(request, protocol, *, timeout=0, check=None, publish=None):
    class Reader(io.RawIOBase):
        def __init__(self, stream):
            self.stream = stream
            self.owner = stream.makefile('rb', buffering=0)
            stream.settimeout(.25)
            self.activity = time.monotonic()

        def readable(self):
            return True

        def readinto(self, buffer):
            while True:
                if check:
                    check()
                if timeout and time.monotonic() - self.activity >= timeout:
                    raise TimeoutError()
                try:
                    data = self.stream.recv(len(buffer))
                except socket.timeout:
                    continue
                buffer[:len(data)] = data
                self.activity = time.monotonic()
                return len(data)

        def close(self):
            try:
                self.owner.close()
            finally:
                super().close()

    class ResponseSocket:
        def __init__(self, stream):
            self.stream = stream

        def makefile(self, *_):
            return io.BufferedReader(Reader(self.stream))

    class Response(http.client.HTTPResponse):
        def __init__(self, stream, *args, **kwargs):
            super().__init__(ResponseSocket(stream), *args, **kwargs)

    def connected(connection):
        if check:
            check()

    class HTTPConnection(http.client.HTTPConnection):
        response_class = Response
        def connect(self):
            super().connect()
            connected(self)

    class HTTPSConnection(http.client.HTTPSConnection):
        response_class = Response
        def connect(self):
            super().connect()
            connected(self)

    class HTTP(HTTPHandler):
        def http_open(self, req):
            return self.do_open(HTTPConnection, req)

    class HTTPS(HTTPSHandler):
        def https_open(self, req):
            return self.do_open(HTTPSConnection, req, context=self._context)

    if check:
        check()
    # Socket polling lives below HTTP buffering so a quiet model can be stopped
    # without poisoning the parser with a timeout or resetting a partial event.
    with build_opener(NoRedirect(), HTTP(), HTTPS()).open(request, timeout=15) as response:
        if response.headers.get_content_type() != 'text/event-stream':
            result = json.loads(response.read())
        else:
            result = events(response, protocol, check, publish)
    if check:
        check()
    return result


def events(response, protocol, check, publish):
    text, calls, blocks, usage, stop = [], {}, {}, {}, None
    complete, payload, published = False, [], 0

    def event(data):
        nonlocal complete, stop, published
        if data == '[DONE]':
            complete = True
            return
        item = json.loads(data)
        if item.get('error') or item.get('type') == 'error':
            raise StreamError('模型流返回错误，已保留当前输出；未执行不完整的工具调用')
        if protocol == 'anthropic':
            kind, index = item.get('type'), item.get('index', 0)
            if kind == 'message_start':
                usage.update(item.get('message', {}).get('usage', {}))
            elif kind == 'content_block_start':
                block = dict(item['content_block'])
                blocks[index] = block
                if block.get('type') == 'text' and block.get('text'):
                    text.append(block['text'])
            elif kind == 'content_block_delta':
                delta = item['delta']
                if delta.get('type') == 'text_delta':
                    fragment = delta.get('text', '')
                    text.append(fragment)
                    blocks[index]['text'] = blocks[index].get('text', '') + fragment
                elif delta.get('type') == 'input_json_delta':
                    blocks[index]['_json'] = blocks[index].get('_json', '') + delta.get('partial_json', '')
            elif kind == 'message_delta':
                usage.update(item.get('usage', {}))
                stop = item.get('delta', {}).get('stop_reason')
            elif kind == 'message_stop':
                complete = True
        else:
            usage.update(item.get('usage') or {})
            for choice in item.get('choices', []):
                if choice.get('index', 0) != 0:
                    continue
                delta = choice.get('delta') or {}
                if delta.get('content'):
                    text.append(delta['content'])
                for call in delta.get('tool_calls') or []:
                    target = calls.setdefault(call['index'], {'id': '', 'type': 'function',
                        'function': {'name': '', 'arguments': ''}})
                    target['id'] += call.get('id') or ''
                    for key in ('name', 'arguments'):
                        target['function'][key] += call.get('function', {}).get(key) or ''
                if choice.get('finish_reason'):
                    stop = choice['finish_reason']
        if publish and text and time.monotonic() - published >= .1:
            publish(''.join(text))
            published = time.monotonic()

    while True:
        if check:
            check()
        line = response.readline()
        if not line:
            if payload:
                event('\n'.join(payload))
            break
        line = line.decode('utf-8').rstrip('\r\n')
        if not line:
            if payload:
                event('\n'.join(payload))
                payload = []
                if complete:
                    break
        elif line.startswith('data:'):
            payload.append(line[5:].lstrip(' '))
    if not complete:
        raise StreamError('模型流意外断开，未执行不完整的工具调用')
    if protocol == 'anthropic':
        content = []
        for index in sorted(blocks):
            block = blocks[index]
            raw = block.pop('_json', None)
            if raw is not None:
                block['input'] = json.loads(raw)
            content.append(block)
        return {'content': content, 'usage': usage, 'stop_reason': stop}
    return {'choices': [{'message': {'content': ''.join(text),
            'tool_calls': [calls[index] for index in sorted(calls)]}, 'finish_reason': stop}], 'usage': usage}
