import json
import shutil
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

import panel_maintenance as pm

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def panel(tmp_path, monkeypatch):
    app, home, backup = (tmp_path / part for part in ('app', 'data', 'backups'))
    for directory in (app, home, backup):
        directory.mkdir()
    (app / 'agent.py').write_text('print("old")\n')
    shutil.copyfile(ROOT / 'release.json', app / 'release.json')
    (app / '.mini-deploy-install').write_text(f'format=2\napp_home={app}\nrelease_fingerprint=sha256:{pm.digest(app / "agent.py")}\n')
    (home / 'request-history.sqlite3').write_bytes(b'BUSINESS DATA')
    (app / 'workspace').mkdir()
    (app / 'workspace/user-file').write_text('BUSINESS FILE')
    env = tmp_path / 'agent.env'
    env.write_text('SECRET=preserve')
    maintenance = pm.Maintenance(app, home, env, tmp_path / 'logs', backup=backup)
    monkeypatch.setattr(maintenance, 'supported', lambda: None)
    monkeypatch.setattr(maintenance, 'exclusive', nullcontext)
    return maintenance


def test_program_snapshot_integrity_and_restore_preserve_business_and_new_files(panel, monkeypatch):
    snapshot = panel.snapshot()
    original = (panel.app / 'agent.py').read_bytes()
    (panel.app / 'agent.py').write_text('new')
    (panel.app / 'extra-module.py').write_text('keep')
    commands = []
    monkeypatch.setattr(pm, 'command', lambda argv, **kwargs: commands.append(argv) or '')
    monkeypatch.setattr(panel, 'health', lambda expected: None)
    panel.restore(snapshot)
    assert (panel.app / 'agent.py').read_bytes() == original
    assert (panel.app / 'extra-module.py').read_text() == 'keep'
    assert (panel.app / 'workspace/user-file').read_text() == 'BUSINESS FILE'
    assert (panel.home / 'request-history.sqlite3').read_bytes() == b'BUSINESS DATA'
    assert panel.env.read_text() == 'SECRET=preserve'
    assert commands == [['systemctl', 'start', 'mini-deploy-agent']]
    (panel.backup / snapshot / 'files/agent.py').write_text('tamper')
    with pytest.raises(ValueError, match='校验失败'):
        panel.restore(snapshot)
    assert (panel.app / 'agent.py').read_bytes() == original


def test_untrusted_snapshot_paths_are_rejected_before_writing(panel):
    snapshot = panel.snapshot()
    path = panel.backup / snapshot / 'program.json'
    data = pm.read_json(path)
    data['files']['../workspace/user-file'] = 'a' * 64
    pm.write_json(path, data)
    with pytest.raises(ValueError, match='非程序路径'):
        panel.verify_program(snapshot)
    for path in ('/etc/passwd', '../agent.py', 'ui/../../data', 'workspace/file', 'ui\\..\\data'):
        assert not pm.allowed(path)


@pytest.mark.parametrize('fail', [False, True])
def test_update_survives_agent_restart_and_recovers_health_failure(panel, tmp_path, monkeypatch, fail):
    old = pm.digest(panel.app / 'agent.py')
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'agent.py').write_text('print("new")\n')
    shutil.copyfile(ROOT / 'release.json', source / 'release.json')
    monkeypatch.setattr(panel, 'stage', lambda job: (source, ['agent.py', 'release.json']))
    commands = []
    monkeypatch.setattr(pm, 'command', lambda argv, **kwargs: commands.append(argv) or '')
    observed = []
    def health(expected):
        observed.append(expected)
        if fail and expected != old:
            raise RuntimeError('new service unhealthy')
    monkeypatch.setattr(panel, 'health', health)
    job = {'id': 'a' * 32, 'action': 'update', 'state': 'queued', 'revision': 'b' * 40,
           'channel': 'gitee', 'settings': panel.settings, 'started_at': time.time()}
    panel.worker(job)
    saved = pm.read_json(panel.directory / (job['id'] + '.json'))
    assert saved['state'] == ('failed' if fail else 'succeeded')
    assert len(list(panel.backup.glob('program-*'))) == 1
    if fail:
        assert saved['recovered'] and pm.digest(panel.app / 'agent.py') == old
        assert observed[-1] == old
        assert commands.count(['systemctl', 'stop', panel.service]) == 2
    else:
        assert panel.version()['revision'] == 'b' * 40
    assert panel.env.read_text() == 'SECRET=preserve'
    assert (panel.home / 'request-history.sqlite3').read_bytes() == b'BUSINESS DATA'
    assert (panel.app / 'workspace/user-file').read_text() == 'BUSINESS FILE'


def test_restore_operation_snapshots_current_version_first(panel, monkeypatch):
    chosen = panel.snapshot()
    (panel.app / 'agent.py').write_text('print("current")')
    original = pm.digest(panel.app / 'agent.py')
    monkeypatch.setattr(pm, 'command', lambda *args, **kwargs: '')
    monkeypatch.setattr(panel, 'health', lambda expected: None)
    job = {'id': 'd' * 32, 'action': 'restore', 'backup': chosen, 'state': 'queued', 'started_at': time.time()}
    panel.worker(job)
    assert job['state'] == 'succeeded'
    assert panel.verify_program(job['recovery_backup'])['version']['fingerprint'] == original


def test_partial_copy_failure_restores_original_program(panel, tmp_path, monkeypatch):
    old = pm.digest(panel.app / 'agent.py')
    source = tmp_path / 'new-source'
    source.mkdir()
    (source / 'agent.py').write_text('print("new")')
    monkeypatch.setattr(panel, 'stage', lambda job: (source, ['agent.py']))
    monkeypatch.setattr(pm, 'command', lambda *args, **kwargs: '')
    monkeypatch.setattr(panel, 'health', lambda expected: None)
    copier = pm.copy_files
    def partial(src, dst, files):
        copier(src, dst, files)
        if src == source:
            raise OSError('disk write failed after partial update')
    monkeypatch.setattr(pm, 'copy_files', partial)
    job = {'id': 'e' * 32, 'action': 'update', 'state': 'queued', 'revision': 'b' * 40,
           'channel': 'gitee', 'started_at': time.time()}
    panel.worker(job)
    assert job['state'] == 'failed' and job['recovered']
    assert pm.digest(panel.app / 'agent.py') == old


def test_restore_refuses_incompatible_state_contract_before_stopping_service(panel, monkeypatch):
    chosen = panel.snapshot()
    pm.write_json(panel.app / 'release.json', {'state_schema': 2})
    calls = []
    monkeypatch.setattr(pm, 'command', lambda argv, **kwargs: calls.append(argv))
    job = {'id': 'f' * 32, 'action': 'restore', 'backup': chosen, 'state': 'queued', 'started_at': time.time()}
    panel.worker(job)
    assert job['state'] == 'failed' and not calls


def test_start_requires_confirmation_pinned_fresh_revision_and_single_job(panel, monkeypatch):
    monkeypatch.setattr(panel, 'status', lambda: {'jobs': []})
    calls = []
    monkeypatch.setattr(pm, 'command', lambda argv, **kwargs: calls.append(argv) or '')
    for action in ({}, {'action': 'backup'}, {'action': ['backup'], 'confirm': True}):
        with pytest.raises(ValueError):
            panel.start(action)
    pm.write_json(panel.directory / 'checked.json', {'revision': 'a' * 40, 'channel': 'gitee', 'checked_at': time.time()})
    with pytest.raises(ValueError, match='过期'):
        panel.start({'action': 'update', 'revision': 'b' * 40, 'channel': 'gitee', 'confirm': True})
    result = panel.start({'action': 'update', 'revision': 'a' * 40, 'channel': 'gitee', 'confirm': True})
    assert result['job'] and calls[0][0] == 'systemd-run'
    assert '--worker' in calls[0] and not any('install.sh' in arg for arg in calls[0])
    monkeypatch.setattr(panel, 'status', lambda: {'jobs': [{'state': 'running'}]})
    with pytest.raises(ValueError, match='正在执行'):
        panel.start({'action': 'backup', 'confirm': True})


def test_stage_refuses_changed_remote_or_contract_and_missing_release(panel, monkeypatch):
    mode = {'revision': 'b' * 40, 'contract': None}
    def command(argv, **kwargs):
        if 'clone' in argv:
            source = Path(argv[-1])
            source.mkdir()
            (source / 'agent.py').write_text('pass')
            (source / 'release.json').write_text(json.dumps(mode['contract'] or pm.read_json(panel.app / 'release.json')))
            return ''
        return mode['revision']
    monkeypatch.setattr(pm, 'command', command)
    with pytest.raises(ValueError, match='远端版本已变化'):
        panel.stage({'id': 'a' * 32, 'channel': 'gitee', 'revision': 'a' * 40})
    mode.update(revision='a' * 40, contract={'state_schema': 2})
    with pytest.raises(ValueError, match='兼容变化'):
        panel.stage({'id': 'b' * 32, 'channel': 'gitee', 'revision': 'a' * 40})
    mode['contract'] = None
    with pytest.raises(ValueError, match='更新包缺少'):
        panel.stage({'id': 'c' * 32, 'channel': 'gitee', 'revision': 'a' * 40})


@pytest.mark.parametrize('channel,error', [
    ('gitee', RuntimeError("fatal: could not read Username for 'https://gitee.com': terminal prompts disabled")),
    ('github', subprocess.TimeoutExpired(['git'], 30)),
])
def test_check_falls_back_to_official_source_and_pins_actual_channel(panel, monkeypatch, channel, error):
    calls = []
    fallback = next(name for name in pm.REMOTES if name != channel)
    def command(argv, **kwargs):
        calls.append(argv)
        assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
        assert kwargs['env']['GIT_CONFIG_GLOBAL'] == pm.os.devnull
        if pm.REMOTES[channel][0] in argv:
            raise error
        return 'b' * 40 + '\trefs/heads/' + pm.REMOTES[fallback][1]
    monkeypatch.setattr(pm, 'command', command)
    result = panel.check(channel)
    assert len(calls) == 2 and result['channel'] == fallback and result['revision'] == 'b' * 40
    assert fallback in result['notice']
    assert pm.read_json(panel.directory / 'checked.json') == result
    monkeypatch.setattr(panel, 'status', lambda: {'jobs': []})
    with pytest.raises(ValueError, match='过期'):
        panel.start({'action': 'update', 'channel': channel, 'revision': result['revision'], 'confirm': True})
    assert not (panel.directory / 'unexpected.json').exists()


def test_check_healthy_source_does_not_contact_mirror_or_fall_back_on_invalid_revision(panel, monkeypatch):
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        return 'a' * 40 + '\trefs/heads/master'
    monkeypatch.setattr(pm, 'command', command)
    assert panel.check('gitee')['channel'] == 'gitee' and len(calls) == 1
    monkeypatch.setattr(pm, 'command', lambda *args, **kwargs: 'invalid revision')
    with pytest.raises(ValueError, match='远端版本读取失败'):
        panel.check('gitee')


def test_unavailable_sources_keep_program_running_and_explain_anonymous_access(panel, monkeypatch):
    original = (panel.app / 'agent.py').read_bytes()
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        raise RuntimeError('fatal: could not read Username: terminal prompts disabled')
    monkeypatch.setattr(pm, 'command', command)
    with pytest.raises(RuntimeError, match='拒绝匿名读取') as error:
        panel.check('gitee')
    assert 'Gitee' in str(error.value) and 'GitHub' in str(error.value)
    assert len(calls) == 2
    job = {'id': '1' * 32, 'channel': 'gitee', 'revision': 'a' * 40, 'action': 'update', 'started_at': time.time()}
    panel.worker(job)
    assert job['state'] == 'failed' and '原程序未修改' in job['error']
    assert not any(argv[0] == 'systemctl' for argv in calls)
    assert (panel.app / 'agent.py').read_bytes() == original


@pytest.mark.parametrize('revision', ['a' * 40, 'b' * 40])
def test_download_fallback_preserves_partial_checkout_and_requires_identical_revision(panel, monkeypatch, revision):
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if 'clone' in argv:
            source = Path(argv[-1])
            source.mkdir()
            (source / 'agent.py').write_text('pass')
            if pm.REMOTES['gitee'][0] in argv:
                raise RuntimeError('fatal: Authentication failed')
            shutil.copyfile(panel.app / 'release.json', source / 'release.json')
            return ''
        return revision
    monkeypatch.setattr(pm, 'command', command)
    monkeypatch.setattr(pm, 'release_files', lambda root: (['agent.py', 'release.json'], 100))
    job = {'id': '2' * 32, 'channel': 'gitee', 'revision': 'a' * 40}
    if revision == job['revision']:
        source, files = panel.stage(job)
        assert source.name == 'source-github' and 'agent.py' in files
        assert job['download_channel'] == 'github' and '同一提交' in job['notice']
    else:
        with pytest.raises(ValueError, match='远端版本已变化'):
            panel.stage(job)
    assert (panel.backup / ('update-' + job['id']) / 'source/agent.py').read_text() == 'pass'
    assert (panel.app / 'agent.py').read_text() == 'print("old")\n'
    assert len(calls) == 3 and all(argv[0] == 'git' for argv in calls)


def test_release_manifest_matches_installer_and_protects_non_program_paths():
    installer = (ROOT / 'install.sh').read_text(encoding='utf-8')
    manifest = json.loads((ROOT / 'release.json').read_text(encoding='utf-8'))['program_entries']
    assert 'mapfile -t RELEASE_ENTRIES' in installer and '"$SOURCE_DIR/release.json"' in installer
    assert set(manifest) == set(pm.ENTRIES)
    for entry in pm.ENTRIES:
        assert (ROOT / entry).exists()
    assert not pm.allowed('projects.json') and not pm.allowed('sites.json')


def test_every_local_python_import_is_shipped():
    import ast
    shipped = {Path(entry).stem for entry in pm.ENTRIES if entry.endswith('.py')}
    local = {path.stem for path in ROOT.glob('*.py')}
    for entry in pm.ENTRIES:
        if not entry.endswith('.py'):
            continue
        tree = ast.parse((ROOT / entry).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            names = ([node.module] if isinstance(node, ast.ImportFrom) else
                     [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            for name in names:
                module = (name or '').split('.')[0]
                assert module not in local or module in shipped, (entry, module)
