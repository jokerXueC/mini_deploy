"""Bound inbound work without imposing a lifetime limit on streamed responses."""
from __future__ import annotations

import io
import socket
import threading
import time
from http.server import ThreadingHTTPServer


class DeadlineInput(io.RawIOBase):
    def __init__(self, connection, seconds=10):
        super().__init__()
        self.connection = connection
        self.deadline = time.monotonic() + seconds
        self.read_budget = 65536

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('HTTP request read deadline exceeded')
        if self.read_budget is not None:
            if self.read_budget <= 0:
                raise TimeoutError('HTTP header byte limit exceeded')
            buffer = memoryview(buffer)[:self.read_budget]
        self.connection.settimeout(remaining)
        count = self.connection.recv_into(buffer)
        if self.read_budget is not None:
            self.read_budget -= count
        return count


class RequestDeadlineMixin:
    def setup(self):
        super().setup()
        self.rfile.close()
        self.rfile = io.BufferedReader(DeadlineInput(self.connection))

    def handle_one_request(self):
        self.rfile.raw.deadline = time.monotonic() + 10
        self.rfile.raw.read_budget = 65536
        return super().handle_one_request()

    def parse_request(self):
        valid = super().parse_request()
        # Uploads get a separate, absolute body deadline, including slow drips.
        self.rfile.raw.deadline = time.monotonic() + 120
        self.rfile.raw.read_budget = None
        return valid

    def end_headers(self):
        # Bound blocked writes, not the overall duration of AI/SSH streams.
        self.connection.settimeout(30)
        return super().end_headers()


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    max_connections = 128
    max_connections_per_ip = 32

    def __init__(self, *args, **kwargs):
        self._connection_lock = threading.Lock()
        self._connections = {}
        self._active_connections = 0
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        ip = client_address[0]
        with self._connection_lock:
            admitted = (self._active_connections < self.max_connections and
                        self._connections.get(ip, 0) < self.max_connections_per_ip)
            if admitted:
                self._active_connections += 1
                self._connections[ip] = self._connections.get(ip, 0) + 1
        if not admitted:
            # Never block the accept loop on a saturated client's response.
            try:
                request.setblocking(False)
                request.send(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nRetry-After: 2\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(ip)
            raise

    def _release(self, ip):
        with self._connection_lock:
            self._active_connections -= 1
            count = self._connections[ip] - 1
            if count:
                self._connections[ip] = count
            else:
                self._connections.pop(ip, None)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        except (OSError, socket.timeout):
            pass
        finally:
            self._release(client_address[0])
