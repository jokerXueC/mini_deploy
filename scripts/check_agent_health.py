"""Bounded local startup acceptance, including the expected running program."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def wait_for_health(agent, *, seconds=30, opener=None, clock=time.monotonic, sleep=time.sleep):
    expected = hashlib.sha256(Path(agent).read_bytes()).hexdigest()
    opener = opener or build_opener(ProxyHandler({}))
    deadline, reason = clock() + seconds, 'no response'
    while clock() < deadline:
        try:
            with opener.open('http://127.0.0.1:6868/health', timeout=min(2, max(.01, deadline - clock()))) as response:
                raw = response.read(4097)
                if len(raw) > 4096:
                    raise ValueError('oversized health response')
                data = json.loads(raw)
                if response.status != 200 or not isinstance(data, dict) or data.get('status') != 'ok':
                    reason = 'invalid health response'
                elif data.get('release') != expected:
                    reason = 'running release does not match installed agent.py'
                else:
                    return
        except (OSError, URLError, ValueError):
            reason = 'health endpoint unavailable or invalid'
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(1, remaining))
    raise RuntimeError(f'Agent startup acceptance failed: {reason}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', type=Path, required=True)
    args = parser.parse_args()
    try:
        wait_for_health(args.agent)
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f'{exc}\n')
    print('Agent health and running release verified.')


if __name__ == '__main__':
    main()
