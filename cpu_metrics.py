"""Linux CPU counters and cached topology, sampled by the existing background worker."""
from __future__ import annotations

import os
import platform
import re
import time
from pathlib import Path


class CpuMetrics:
    def __init__(self):
        self.previous = {}
        self.topology = {}
        self.topology_at = 0.0
        self.topology_ids = ()
        self.snapshot = {}

    def _topology(self, ids):
        now = time.monotonic()
        if self.topology and self.topology_ids == ids and now - self.topology_at < 60:
            return self.topology
        processors, model = {}, ''
        try:
            text = Path('/proc/cpuinfo').read_text(encoding='utf-8')
            for block in re.split(r'\n\s*\n', text):
                fields = dict(line.split(':', 1) for line in block.splitlines() if ':' in line)
                fields = {key.strip().lower(): value.strip() for key, value in fields.items()}
                model = model or fields.get('model name', '') or fields.get('hardware', '')
                identifier = fields.get('processor', '')
                if identifier.isdigit():
                    processors[int(identifier)] = fields
        except (OSError, ValueError):
            pass
        cores, sockets = set(), set()
        complete = bool(ids)
        for identifier in ids:
            fields = processors.get(identifier, {})
            socket, core = fields.get('physical id'), fields.get('core id')
            if socket is None or core is None:
                complete = False
            else:
                cores.add((socket, core))
                sockets.add(socket)
        self.topology = {'model': model or platform.machine() or 'CPU',
                         'logical_count': len(ids) or os.cpu_count(),
                         'physical_cores': len(cores) if complete else None,
                         'sockets': len(sockets) if complete else None}
        self.topology_at, self.topology_ids = now, ids
        return self.topology

    def sample(self):
        counters = {}
        try:
            for line in Path('/proc/stat').read_text(encoding='utf-8').splitlines():
                parts = line.split()
                if not parts or not re.fullmatch(r'cpu(?:[0-9]+)?', parts[0]):
                    continue
                # guest/guest_nice already contribute to user/nice; count only the first eight fields.
                values = [int(value) for value in parts[1:9]]
                if len(values) < 4 or any(value < 0 for value in values):
                    continue
                counters[parts[0]] = (sum(values), values[3] + (values[4] if len(values) > 4 else 0))
        except (OSError, ValueError):
            counters = {}

        def usage(key):
            before, after = self.previous.get(key), counters.get(key)
            if before is None or after is None:
                return None
            total, idle = after[0] - before[0], after[1] - before[1]
            if total <= 0 or idle < 0 or idle > total:
                return None
            return round(max(0.0, min(100.0, (1 - idle / total) * 100)), 1)

        ids = tuple(sorted(int(key[3:]) for key in counters if key != 'cpu'))
        self.snapshot = {**self._topology(ids), 'percent': usage('cpu'),
                         'cores': [{'id': identifier, 'percent': usage(f'cpu{identifier}')} for identifier in ids]}
        self.previous = counters
        return self.snapshot['percent']
