"""Distribution identity and real host prerequisites, without host mutations."""
import pytest

import host_platform
from test_installer_operations import ROOT, bash_run, function


def host(tmp_path, monkeypatch, identifier='debian', version='12', **changes):
    paths = {'etc/os-release': f'ID={identifier}\nVERSION_ID="{version}"\nPRETTY_NAME="Test Linux"\n',
             'proc/1/comm': 'systemd\n', 'proc/meminfo': 'MemTotal: 4096 kB\n'}
    paths.update(changes)
    for name, value in paths.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
    monkeypatch.setattr(host_platform.shutil, 'which', lambda name: '/usr/bin/' + name if name in {
        'systemctl', 'systemd-analyze', 'apt-get'} else None)
    return host_platform.inspect_host(root=tmp_path, platform_name='linux', uid=0, architecture='aarch64')


@pytest.mark.parametrize('identifier,version,tier', [
    ('debian', '12', 'recommended'), ('debian', '13', 'recommended'),
    ('ubuntu', '22.04', 'recommended'), ('ubuntu', '24.04', 'recommended'),
    ('rocky', '9.5', 'experimental'), ('almalinux', '9.5', 'experimental'),
    ('rhel', '10', 'experimental'), ('fedora', '43', 'experimental'),
    ('debian', '11', 'experimental'), ('ubuntu', '20.04', 'experimental'),
    ('custom', '1', 'experimental'),
])
def test_distribution_tiers_do_not_pretend_every_linux_was_validated(tmp_path, monkeypatch, identifier, version, tier):
    result = host(tmp_path, monkeypatch, identifier, version)
    assert result['tier'] == tier
    assert result['state'] == ('available' if tier == 'recommended' else 'pending')
    assert result['architecture'] == 'aarch64'


@pytest.mark.parametrize('init', ['bash', 'init', 'openrc-init', 'tini'])
def test_non_systemd_pid_one_is_rejected_even_when_systemctl_exists(tmp_path, monkeypatch, init):
    result = host(tmp_path, monkeypatch, **{'proc/1/comm': init})
    assert result['state'] == 'unsupported'
    assert any('PID 1' in error for error in result['errors'])


def test_container_and_selinux_are_reported_without_changing_them(tmp_path, monkeypatch):
    result = host(tmp_path, monkeypatch, **{'.dockerenv': '', 'sys/fs/selinux/enforce': '1'})
    assert result['container'] and result['state'] == 'unsupported'
    assert result['selinux'] == 'enforcing'
    assert (tmp_path / 'sys/fs/selinux/enforce').read_text() == '1'
    (tmp_path / '.dockerenv').unlink()
    result = host_platform.inspect_host(root=tmp_path, platform_name='linux', uid=0)
    assert result['state'] == 'pending' and not result['errors']


def test_release_file_is_parsed_as_data_and_falls_back_to_usr_lib(tmp_path):
    marker = tmp_path / 'must-not-exist'
    release = tmp_path / 'usr/lib/os-release'
    release.parent.mkdir(parents=True)
    release.write_text(f'ID=rocky\nVERSION_ID="9.5"\nPRETTY_NAME="$(touch {marker.as_posix()})"\nBAD="unterminated\n')
    result = host_platform.os_release(tmp_path)
    assert result['ID'] == 'rocky' and result['VERSION_ID'] == '9.5'
    assert '$(touch' in result['PRETTY_NAME'] and not marker.exists()


def test_unprivileged_and_non_linux_are_not_host_installations(tmp_path, monkeypatch):
    host(tmp_path, monkeypatch)
    result = host_platform.inspect_host(root=tmp_path, platform_name='linux', uid=1000)
    assert result['state'] == 'unsupported'
    assert any('root' in error for error in result['errors'])
    result = host_platform.inspect_host(root=tmp_path, platform_name='win32', uid=0)
    assert result['state'] == 'unsupported' and result['id'] == 'unknown'


def runtime_script(body, **env):
    selector = (ROOT / 'scripts/python-runtime.sh').read_text(encoding='utf-8')
    return bash_run(selector + '\n' + body, **env)


def test_runtime_selection_falls_back_from_old_system_python_without_changing_it():
    result = runtime_script('''
command() { if [[ "$1" == -v ]]; then printf '%s\n' "$2"; else builtin command "$@"; fi; }
readlink() { printf '%s\n' "$3"; }
python3() { return 1; }
python3.13() { return 1; }
python3.12() { return 1; }
python3.11() { return 0; }
select_python
''', PYTHON_BIN='')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'python3.11'


def test_explicit_broken_python_never_silently_switches_to_another_runtime():
    result = runtime_script('''
command() { printf '%s\n' "$2"; }
readlink() { printf '%s\n' "$3"; }
chosen_python() { return 1; }
select_python
''', PYTHON_BIN='chosen_python')
    assert result.returncode != 0
    assert 'SQLite and SSL' in result.stderr


def test_check_only_exits_before_manifest_lock_or_installation_changes():
    installer = (ROOT / 'install.sh').read_text(encoding='utf-8')
    stop = installer.index('if [[ "$INSTALL_CHECK_ONLY" == "true" ]]; then exit 0; fi')
    assert stop < installer.index('\nacquire_maintenance_lock\n')
    assert stop < installer.index('RELEASE_MANIFEST=')
    assert '"$PYTHON_BIN" "$SOURCE_DIR/host_platform.py" --check' in installer[:stop]


@pytest.mark.parametrize('explicit,trusted,expected', [
    (False, True, 'saved_python'), (True, True, 'chosen_python'), (False, False, None),
])
def test_upgrade_uses_saved_runtime_before_probing_old_default_and_rejects_untrusted_config(tmp_path, explicit, trusted, expected):
    env = tmp_path / 'agent.env'
    env.write_text('DEPLOY_AGENT_PYTHON_BIN=saved_python\n', encoding='utf-8')
    installer = (ROOT / 'install.sh').read_text(encoding='utf-8')
    start = installer.index('# Read the saved runtime only after validating')
    end = installer.index('\n"$PYTHON_BIN" "$SOURCE_DIR/host_platform.py" --check', start)
    result = runtime_script(function('env_file_value') + '''
command() { if [[ "$1" == -v ]]; then printf '%s\n' "$2"; else builtin command "$@"; fi; }
readlink() { printf '%s\n' "$3"; }
assert_no_symlink_components() { return 0; }
assert_trusted_directory_chain() { [[ "$TRUSTED" == true ]]; }
validate_existing_control_file() { [[ "$TRUSTED" == true ]]; }
python3() { echo default-must-not-run >&2; return 1; }
saved_python() { return 0; }
chosen_python() { return 0; }
''' + installer[start:end] + '\nprintf "%s\\n" "$PYTHON_BIN"',
        ENV_FILE=env, PYTHON_BIN='chosen_python' if explicit else '',
        PYTHON_BIN_EXPLICIT=str(explicit).lower(), TRUSTED=str(trusted).lower())
    if expected:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected
        assert 'default-must-not-run' not in result.stderr
    else:
        assert result.returncode != 0 and not result.stdout.strip()


@pytest.mark.parametrize('explicit', [False, True])
def test_manual_backup_uses_saved_interpreter_unless_explicitly_overridden(tmp_path, explicit):
    env = tmp_path / 'agent.env'
    env.write_text('DEPLOY_AGENT_PYTHON_BIN=saved_python\n', encoding='utf-8')
    backup = (ROOT / 'scripts/backup-installation.sh').read_text(encoding='utf-8')
    start = backup.index('env_file_value() {')
    end = backup.index('# Archive new site/monitoring data', start)
    assert backup.index('Backup control file must be root-owned') < start
    result = runtime_script(r'''
command() { if [[ "$1" == -v ]]; then printf '%s\n' "$2"; else builtin command "$@"; fi; }
readlink() { printf '%s\n' "$3"; }
saved_python() { return 0; }
chosen_python() { return 0; }
''' + backup[start:end] + '\nprintf "%s\\n" "$PYTHON_BIN"',
        ENV_FILE=env, PYTHON_BIN='chosen_python' if explicit else '')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ('chosen_python' if explicit else 'saved_python')
