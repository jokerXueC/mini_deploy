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
    for entry in pm.INSTALLATION_FILES:
        (app / entry).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / entry, app / entry)
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
    shutil.copyfile(panel.app / 'release.json', source / 'release.json')
    monkeypatch.setattr(panel, 'stage', lambda job: (source, ['agent.py']))
    monkeypatch.setattr(pm, 'command', lambda *args, **kwargs: '')
    monkeypatch.setattr(panel, 'health', lambda expected: None)
    copier = pm.copy_files
    def partial(src, dst, files, **kwargs):
        copier(src, dst, files, **kwargs)
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
            for entry in pm.INSTALLATION_FILES:
                (source / entry).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(panel.app / entry, source / entry)
            return ''
        return revision
    monkeypatch.setattr(pm, 'command', command)
    monkeypatch.setattr(pm, 'release_files', lambda root, **kwargs: (['agent.py', 'release.json'], 100))
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


@pytest.fixture
def additive_update(panel, tmp_path, monkeypatch):
    legacy = pm.read_json(panel.app / 'release.json')
    legacy['program_entries'] = ['agent.py', 'panel_maintenance.py', 'release.json', 'ui', *pm.INSTALLATION_FILES]
    pm.write_json(panel.app / 'release.json', legacy)
    (panel.app / 'panel_maintenance.py').write_text('VERSION = "old"\n')
    (panel.app / 'ui').mkdir()
    (panel.app / 'ui/index.html').write_text('old UI')
    monkeypatch.setattr(pm, 'ENTRIES', tuple(legacy['program_entries']))
    repository = tmp_path / 'repository'
    shutil.copytree(panel.app, repository)
    target = {**legacy, 'program_entries': [*legacy['program_entries'], 'setup_checks.py', 'scripts/check_agent_health.py']}
    pm.write_json(repository / 'release.json', target)
    (repository / 'agent.py').write_text('from setup_checks import VALUE\nprint(VALUE)\n')
    (repository / 'setup_checks.py').write_text('VALUE = "new module installed"\n')
    (repository / 'scripts').mkdir()
    (repository / 'scripts/check_agent_health.py').write_text('print("health helper installed")\n')
    (repository / 'ui/index.html').write_text('new UI')
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if 'clone' in argv:
            shutil.copytree(repository, Path(argv[-1]))
        elif 'rev-parse' in argv:
            return 'b' * 40
        return ''
    monkeypatch.setattr(pm, 'command', command)
    monkeypatch.setattr(panel, 'health', lambda expected: None)
    job = {'id': '3' * 32, 'action': 'update', 'state': 'queued', 'revision': 'b' * 40,
           'channel': 'github', 'started_at': time.time()}
    return repository, legacy, target, job, calls


def test_additive_manifest_update_installs_new_modules_and_restores_old_snapshot(panel, additive_update, monkeypatch):
    repository, legacy, target, job, calls = additive_update
    original = {name: (panel.app / name).read_bytes() for name in ('agent.py', 'release.json', '.mini-deploy-install')}
    panel.worker(job)
    assert job['state'] == 'succeeded', job
    assert pm.read_json(panel.app / 'release.json') == target
    for entry in ('setup_checks.py', 'scripts/check_agent_health.py', 'ui/index.html'):
        assert (panel.app / entry).read_bytes() == (repository / entry).read_bytes()
    result = subprocess.run([pm.sys.executable, str(panel.app / 'agent.py')], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'new module installed'
    # A fresh maintenance process imports the new manifest; the old snapshot must still work.
    monkeypatch.setattr(pm, 'ENTRIES', tuple(target['program_entries']))
    panel.restore(job['backup'])
    for entry, content in original.items():
        assert (panel.app / entry).read_bytes() == content
    assert pm.read_json(panel.app / 'release.json') == legacy
    assert (panel.app / 'setup_checks.py').is_file()  # Rollback never deletes files.
    assert (panel.app / 'workspace/user-file').read_text() == 'BUSINESS FILE'
    assert panel.env.read_text() == 'SECRET=preserve'
    assert (panel.home / 'request-history.sqlite3').read_bytes() == b'BUSINESS DATA'
    assert ['systemctl', 'stop', panel.service] in calls


@pytest.mark.parametrize('failure', ['health', 'partial', 'corrupt-manifest'])
def test_additive_update_failure_restores_and_allows_retry(panel, additive_update, monkeypatch, failure):
    repository, legacy, target, job, calls = additive_update
    original = {name: (panel.app / name).read_bytes() for name in ('agent.py', 'release.json', '.mini-deploy-install', 'ui/index.html')}
    old = pm.digest(panel.app / 'agent.py')
    copier = pm.copy_files
    def partial(src, dst, files, **kwargs):
        copier(src, dst, files, **kwargs)
        if src.name == 'source':
            if failure == 'corrupt-manifest':
                (dst / 'release.json').write_text('{broken')
            raise OSError('partial update failed')
    def health(expected):
        if expected != old:
            raise RuntimeError('unhealthy new service')
    if failure == 'health':
        monkeypatch.setattr(panel, 'health', health)
    else:
        monkeypatch.setattr(pm, 'copy_files', partial)
    panel.worker(job)
    assert job['state'] == 'failed' and job.get('recovered'), job
    for entry, content in original.items():
        assert (panel.app / entry).read_bytes() == content
    assert (panel.app / 'setup_checks.py').is_file()
    # Retry a newer version of the retained module without adopting arbitrary user files.
    (repository / 'setup_checks.py').write_text('VALUE = "retry installed"\n')
    monkeypatch.setattr(pm, 'copy_files', copier)
    monkeypatch.setattr(panel, 'health', lambda expected: None)
    retry = {**job, 'id': '4' * 32, 'state': 'queued'}
    panel.worker(retry)
    assert retry['state'] == 'succeeded', retry
    assert (panel.app / 'setup_checks.py').read_bytes() == (repository / 'setup_checks.py').read_bytes()
    assert (panel.home / 'request-history.sqlite3').read_bytes() == b'BUSINESS DATA'
    assert panel.env.read_text() == 'SECRET=preserve'


@pytest.mark.parametrize('modified', [False, True])
def test_new_module_collision_never_overwrites_unmanaged_or_edited_files(panel, additive_update, modified):
    repository, legacy, target, job, calls = additive_update
    if modified:
        panel.worker(job)
        assert job['state'] == 'succeeded', job
        panel.restore(job['backup'])
        calls.clear()
        job = {**job, 'id': '5' * 32, 'state': 'queued'}
    path = panel.app / 'setup_checks.py'
    path.write_text('USER CODE - preserve')
    panel.worker(job)
    assert job['state'] == 'failed' and '不属于原程序清单' in job['error']
    assert job['phase'] == '更新未执行，当前服务和程序未改动'
    assert not any(argv[0] == 'systemctl' for argv in calls)
    assert path.read_text() == 'USER CODE - preserve'


@pytest.mark.parametrize('change', ['state_schema', 'python_min', 'requirements', 'service', 'remove-requirements'])
def test_real_installation_changes_refused_before_stopping_service(panel, additive_update, change):
    repository, legacy, target, job, calls = additive_update
    if change == 'state_schema':
        target['state_schema'] += 1
    elif change == 'python_min':
        target['python_min'] = [99, 0]
    elif change == 'remove-requirements':
        target['program_entries'].remove('requirements-ssh.txt')
    else:
        path = repository / ('requirements-ssh.txt' if change == 'requirements' else 'systemd/mini-deploy-agent.service')
        path.write_text(path.read_text() + '\n# changed installation\n')
    pm.write_json(repository / 'release.json', target)
    panel.worker(job)
    assert job['state'] == 'failed' and 'install.sh' in job['error'], job
    assert not any(argv[0] == 'systemctl' for argv in calls)
    assert pm.read_json(panel.app / 'release.json') == legacy


@pytest.mark.parametrize('entry', ['../agent.py', '/etc/passwd', 'scripts/../agent.py', 'projects.json',
                                  'workspace', 'workspace/code.py', 'ui//x', 'ui\\x', 'agent.py'])
def test_invalid_target_manifest_paths_refused_without_service_stop(panel, additive_update, entry):
    repository, legacy, target, job, calls = additive_update
    target['program_entries'].append(entry)
    pm.write_json(repository / 'release.json', target)
    panel.worker(job)
    assert job['state'] == 'failed' and '程序文件清单' in job['error'], job
    assert not any(argv[0] == 'systemctl' for argv in calls)


def test_manifest_file_cannot_be_a_directory(panel, additive_update):
    repository, legacy, target, job, calls = additive_update
    target['program_entries'].append('directory.py')
    (repository / 'directory.py').mkdir()
    (repository / 'directory.py/data').write_text('not program code')
    pm.write_json(repository / 'release.json', target)
    panel.worker(job)
    assert job['state'] == 'failed' and '不是普通文件' in job['error']
    assert not any(argv[0] == 'systemctl' for argv in calls)


def test_restore_refuses_changed_dependencies_with_same_metadata(panel, additive_update):
    chosen = panel.snapshot()
    (panel.app / 'requirements-ssh.txt').write_text('new dependency environment')
    calls = additive_update[-1]
    job = {'id': '6' * 32, 'action': 'restore', 'backup': chosen, 'state': 'queued', 'started_at': time.time()}
    panel.worker(job)
    assert job['state'] == 'failed' and 'install.sh' in job['error'], job
    assert not calls


def test_module_only_failure_restores_original_version_receipt(panel, additive_update, monkeypatch):
    repository, legacy, target, job, calls = additive_update
    shutil.copyfile(panel.app / 'agent.py', repository / 'agent.py')
    original = panel.version()
    observed = []
    def health(expected):
        observed.append(expected)
        if len(observed) == 1:
            raise RuntimeError('module startup failed')
    monkeypatch.setattr(panel, 'health', health)
    panel.worker(job)
    assert job['state'] == 'failed' and job.get('recovered'), job
    assert panel.version() == original


def test_shipped_release_installs_both_added_files_from_legacy_manifest(panel, monkeypatch):
    target = pm.read_json(ROOT / 'release.json')
    added = {'setup_checks.py', 'scripts/check_agent_health.py'}
    legacy = {**target, 'program_entries': [p for p in target['program_entries'] if p not in added]}
    for entry in legacy['program_entries']:
        source, destination = ROOT / entry, panel.app / entry
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copyfile(source, destination)
    pm.write_json(panel.app / 'release.json', legacy)
    monkeypatch.setattr(pm, 'ENTRIES', tuple(legacy['program_entries']))
    def command(argv, **kwargs):
        if 'clone' in argv:
            checkout = Path(argv[-1])
            for entry in target['program_entries']:
                source, destination = ROOT / entry, checkout / entry
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, destination)
                else:
                    shutil.copyfile(source, destination)
        elif 'rev-parse' in argv:
            return 'b' * 40
        return ''
    monkeypatch.setattr(pm, 'command', command)
    monkeypatch.setattr(panel, 'health', lambda expected: None)
    job = {'id': '7' * 32, 'action': 'update', 'revision': 'b' * 40, 'channel': 'github', 'started_at': time.time()}
    panel.worker(job)
    assert job['state'] == 'succeeded', job
    for entry in added:
        assert pm.digest(panel.app / entry) == pm.digest(ROOT / entry)
    result = subprocess.run([pm.sys.executable, '-c', 'import setup_checks; import scripts.check_agent_health'],
                            cwd=panel.app, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert pm.read_json(panel.app / 'release.json') == target


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
