import json
import threading
import time
from types import SimpleNamespace

import pytest

import agent
import host_swap
from test_deployment_removal import http_server as http_server, runtime as runtime
from test_ssh_terminal import request


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(host_swap, 'SIZE', 2 * 1024**2)
    proc = tmp_path / 'proc'
    proc.mkdir()
    (proc / 'meminfo').write_text('MemTotal: 2097152 kB\nMemAvailable: 1048576 kB\n')
    fstab = tmp_path / 'fstab'
    original = '# existing config\nUUID=root / ext4 defaults 0 1\n'
    fstab.write_bytes(original.encode())
    state = {'active': [], 'commands': [], 'fail': '', 'filesystem': 'ext4', 'fills': 0}

    def active():
        (proc / 'swaps').write_text('Filename Type Size Used Priority\n' + ''.join(
            f'{path} file {host_swap.SIZE // 1024} {host_swap.SIZE // 2048} -2\n' for path in state['active']))

    def run(command, *, timeout):
        state['commands'].append(command)
        if command[0] == state['fail']:
            raise RuntimeError(f'{command[0]} failed')
        if command[0] == 'findmnt':
            return state['filesystem']
        if command[0] == 'swapon':
            state['active'].append(command[1])
        elif command[0] == 'swapoff':
            state['active'].remove(command[1])
        elif command[0] != 'mkswap':
            pytest.fail(str(command))
        active()
        return ''

    manager = host_swap.Manager(tmp_path / 'data', proc=proc, fstab=fstab, runner=run, supported=True, directory=tmp_path / 'swap')
    def fill(stream):
        state['fills'] += 1
        # Test with a small file; production writes all 2 GiB to avoid swapfile holes.
        stream.truncate(host_swap.SIZE)
    monkeypatch.setattr(manager, '_fill', fill)
    monkeypatch.setattr(host_swap.shutil, 'disk_usage', lambda path: SimpleNamespace(free=8 * 1024**3))
    active()
    return manager, state, active, original


def test_enable_disable_and_reenable_preserve_file_and_external_configuration(engine):
    manager, state, _, original = engine
    assert manager.status()['editable'] and not manager.status()['enabled']
    manager._apply(True)
    status = manager.status()
    assert status['enabled'] and status['managed_active'] and status['persistent']
    assert manager.fstab.read_text().startswith(original)
    assert host_swap.TAG in manager.fstab.read_text()
    owner = json.loads(manager.record.read_text())
    assert owner['ready']
    restored = host_swap.Manager(manager.home, proc=manager.proc, fstab=manager.fstab, runner=manager.run,
                                 supported=True, directory=manager.directory)
    assert restored.status()['managed_active']
    manager._apply(False)
    assert manager.fstab.read_text() == original and not manager.status()['enabled']
    assert manager.path.exists() and manager.record.exists()
    manager._apply(True)
    assert state['fills'] == 1 and sum(c[0] == 'mkswap' for c in state['commands']) == 1
    assert len(list(manager.directory.glob('fstab-*.bak'))) == 3
    assert all(command[0] not in {'rm', 'reboot', 'systemctl'} and '-a' not in command for command in state['commands'])


@pytest.mark.parametrize('configured', [False, True])
def test_unresolved_system_swap_is_not_modified_or_duplicated(engine, configured):
    manager, state, active, original = engine
    if configured:
        manager.fstab.write_text(original + '/existing-swap none swap sw 0 0\n')
    else:
        state['active'] = ['/existing-swap']
        active()
    before = manager.fstab.read_bytes()
    assert not manager.status()['editable']
    with pytest.raises(ValueError):
        manager._apply(True)
    with pytest.raises(ValueError):
        manager.start(False)
    assert not state['commands'] and manager.fstab.read_bytes() == before


def external_swap(engine, count=1):
    manager, state, active, original = engine
    paths = [manager.home.parent / f'existing-swap-{index}' for index in range(count)]
    for path in paths:
        path.write_bytes(b'preserved external swap content')
    state['active'] = [str(path) for path in paths]
    active()
    manager.fstab.write_text(original + ''.join(f'{path} none swap sw 0 0\n' for path in paths))
    return paths


def test_existing_swap_can_be_disabled_and_reenabled_after_agent_restart(engine):
    manager, state, _, _ = engine
    paths = external_swap(engine, 2)
    before = manager.fstab.read_bytes()
    assert manager.status()['editable'] and manager.status()['mode'] == 'external'
    manager._apply(False)
    assert not state['active'] and not manager.status()['enabled'] and manager.status()['editable']
    assert manager.external_record.exists() and not manager.path.exists()
    restored = host_swap.Manager(manager.home, proc=manager.proc, fstab=manager.fstab, runner=manager.run,
                                 supported=True, directory=manager.directory)
    restored._apply(True)
    assert set(state['active']) == {str(path) for path in paths}
    assert manager.fstab.read_bytes() == before
    assert all(path.read_bytes() == b'preserved external swap content' for path in paths)
    assert [command[0] for command in state['commands']] == ['swapoff', 'swapoff', 'swapon', 'swapon']


def test_existing_swap_memory_guard_and_failure_preserve_files_and_configuration(engine):
    manager, state, _, _ = engine
    paths = external_swap(engine)
    before = manager.fstab.read_bytes()
    (manager.proc / 'meminfo').write_text('MemTotal: 2097152 kB\nMemAvailable: 1024 kB\n')
    with pytest.raises(ValueError, match='内存不足'):
        manager._apply(False)
    assert not state['commands'] and not manager.external_record.exists()
    (manager.proc / 'meminfo').write_text('MemTotal: 2097152 kB\nMemAvailable: 1048576 kB\n')
    state['fail'] = 'swapoff'
    with pytest.raises(RuntimeError):
        manager._apply(False)
    assert state['active'] == [str(paths[0])] and manager.fstab.read_bytes() == before
    assert paths[0].exists() and manager.external_record.exists()


def test_external_swap_partial_disable_failure_restores_already_disabled_sources(engine, monkeypatch):
    manager, state, _, _ = engine
    paths = external_swap(engine, 2)
    runner = manager.run

    def fail_second(command, **kwargs):
        if command == ['swapoff', str(paths[1])]:
            raise RuntimeError('second swapoff failed')
        return runner(command, **kwargs)

    monkeypatch.setattr(manager, 'run', fail_second)
    with pytest.raises(RuntimeError, match='second swapoff'):
        manager._apply(False)
    assert set(state['active']) == {str(path) for path in paths}
    assert all(path.exists() for path in paths)


def test_replaced_external_swap_is_not_reenabled(engine):
    manager, state, _, _ = engine
    paths = external_swap(engine)
    manager._apply(False)
    paths[0].rename(paths[0].with_suffix('.saved'))
    paths[0].write_text('different business file')
    calls = len(state['commands'])
    assert not manager.status()['editable']
    with pytest.raises(ValueError, match='身份已变化'):
        manager._apply(True)
    assert len(state['commands']) == calls and paths[0].read_text() == 'different business file'


def test_partial_external_restore_exposes_retry_without_allocating_or_formatting(engine):
    manager, state, _, _ = engine
    paths = external_swap(engine, 2)
    manager._apply(False)
    state['active'] = [str(paths[0])]
    (manager.proc / 'swaps').write_text('Filename Type Size Used Priority\n' + f'{paths[0]} file 2048 0 -2\n')
    status = manager.status()
    assert status['enabled'] and status['restore_pending'] and status['editable']
    manager._apply(True)
    assert set(state['active']) == {str(path) for path in paths}
    assert not manager.status()['restore_pending'] and state['fills'] == 0
    assert all(command[0] in {'swapon', 'swapoff'} for command in state['commands'])


def test_invalid_external_restore_record_keeps_actual_active_status(engine):
    manager, state, _, _ = engine
    external_swap(engine)
    manager.external_record.parent.mkdir(parents=True)
    manager.external_record.write_text('[]')
    status = manager.status()
    assert status['enabled'] and not status['editable'] and '记录无效' in status['detail']
    assert not state['commands']


def test_low_disk_unsupported_filesystem_and_unowned_file_are_kept(engine, monkeypatch):
    manager, state, _, original = engine
    state['filesystem'] = 'btrfs'
    with pytest.raises(ValueError, match='文件系统'):
        manager._apply(True)
    state['filesystem'] = 'xfs'
    monkeypatch.setattr(host_swap.shutil, 'disk_usage', lambda path: SimpleNamespace(free=host_swap.SIZE))
    with pytest.raises(ValueError, match='空间不足'):
        manager._apply(True)
    manager.path.write_text('business file')
    with pytest.raises(ValueError, match='非托管文件'):
        manager._apply(True)
    assert manager.path.read_text() == 'business file' and manager.fstab.read_text() == original


def test_memory_guard_and_failed_swapoff_keep_boot_entry(engine):
    manager, state, _, _ = engine
    manager._apply(True)
    before = manager.fstab.read_bytes()
    (manager.proc / 'meminfo').write_text('MemTotal: 2097152 kB\nMemAvailable: 1024 kB\n')
    with pytest.raises(ValueError, match='内存不足'):
        manager._apply(False)
    assert not any(c[0] == 'swapoff' for c in state['commands'])
    (manager.proc / 'meminfo').write_text('MemTotal: 2097152 kB\nMemAvailable: 1048576 kB\n')
    state['fail'] = 'swapoff'
    with pytest.raises(RuntimeError):
        manager._apply(False)
    assert manager.fstab.read_bytes() == before and manager.status()['managed_active']


@pytest.mark.parametrize('failure', ['mkswap', 'swapon'])
def test_failed_enable_preserves_files_and_does_not_add_boot_entry(engine, failure):
    manager, state, _, original = engine
    state['fail'] = failure
    with pytest.raises(RuntimeError):
        manager._apply(True)
    assert manager.path.exists() and manager.fstab.read_text() == original
    if failure == 'mkswap':
        state['fail'] = ''
        with pytest.raises(ValueError, match='未完成'):
            manager._apply(True)
        assert state['fills'] == 1


def test_modified_ownership_and_foreign_fstab_entry_are_not_overwritten(engine):
    manager, state, _, original = engine
    manager._apply(True)
    manager.fstab.write_text(original + f'{manager.path} none swap pri=99 0 0\n')
    before = manager.fstab.read_bytes()
    with pytest.raises(ValueError, match='不同的 Swap'):
        manager._apply(True)
    assert manager.fstab.read_bytes() == before
    data = json.loads(manager.record.read_text())
    data['inode'] += 1
    manager.record.write_text(json.dumps(data))
    calls = len(state['commands'])
    with pytest.raises(ValueError, match='身份'):
        manager._apply(False)
    assert len(state['commands']) == calls and manager.path.exists()


def test_boot_config_failure_keeps_active_swap_and_retry_does_not_reformat(engine, monkeypatch):
    manager, state, _, original = engine
    persist = manager._persist
    def fail(enabled):
        raise OSError('fstab is read-only')
    monkeypatch.setattr(manager, '_persist', fail)
    with pytest.raises(OSError):
        manager._apply(True)
    assert manager.status()['managed_active'] and not manager.status()['persistent']
    assert manager.fstab.read_text() == original
    monkeypatch.setattr(manager, '_persist', persist)
    manager._apply(True)
    assert manager.status()['persistent'] and state['fills'] == 1
    assert sum(c[0] == 'swapon' for c in state['commands']) == 1


def test_incomplete_ownership_and_unknown_environment_never_look_editable(engine):
    manager, state, _, _ = engine
    manager.directory.mkdir()
    manager.record.write_text('[]')
    assert not manager.status()['editable']
    assert not state['commands']
    manager.supported = False
    assert manager.status()['supported'] is False


def test_async_status_rejects_duplicate_requests_and_records_audit(engine, monkeypatch):
    manager, _, _, _ = engine
    waiting, release = threading.Event(), threading.Event()
    original = manager._apply
    def apply(enabled):
        waiting.set()
        assert release.wait(5)
        original(enabled)
    monkeypatch.setattr(manager, '_apply', apply)
    audited = []
    assert manager.start(True, audit=audited.append)['job']['state'] == 'running'
    assert waiting.wait(2)
    with pytest.raises(ValueError, match='正在进行'):
        manager.start(True)
    release.set()
    deadline = time.monotonic() + 5
    while manager.status()['job']['state'] == 'running' and time.monotonic() < deadline:
        time.sleep(.01)
    assert manager.status()['job']['state'] == 'succeeded' and audited == [True]


def test_routes_require_login_csrf_and_strict_payload(http_server, runtime, engine, monkeypatch):
    manager, _, _, _ = engine
    monkeypatch.setattr(agent, '_host_swap', lambda: manager)
    for method in ('GET', 'POST'):
        assert request(http_server, method, '/system/swap', {'enabled': True}, authenticated=False)[0] == 401
    assert request(http_server, 'POST', '/system/swap', {'enabled': True}, csrf=False)[0] == 403
    assert request(http_server, 'GET', '/system/swap')[1]['editable']
    for payload in ({}, {'enabled': 'yes'}, {'enabled': True, 'path': '/business'}):
        assert request(http_server, 'POST', '/system/swap', payload)[0] == 400
    assert request(http_server, 'POST', '/system/swap', {'enabled': True})[0] == 202
    deadline = time.monotonic() + 5
    while manager.status()['job'].get('state') == 'running' and time.monotonic() < deadline:
        time.sleep(.01)
    assert manager.status()['enabled']
