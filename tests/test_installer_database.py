"""Database dependencies use a marked private runtime and retain a working fallback."""
import os

from test_installer_operations import bash_run, function


def prepare(tmp_path, *, fail=False, skip=False, unmarked=False):
    app = tmp_path / 'panel'
    app.mkdir()
    runtime = tmp_path / 'panel-runtime'
    if unmarked:
        runtime.mkdir()
        (runtime / 'keep.txt').write_text('preserve')
    fake = tmp_path / 'fake-python'
    fake.write_text('''#!/usr/bin/env bash
set -eu
if [[ "$1" == -m && "$2" == pip ]]; then
  if [[ "$*" == *" install "* ]]; then
    printf 'PIP %s\\n' "$*" >>"$CALLS"
    if [[ "$FAIL" == true ]]; then exit 1; fi
    printf ready >"$0.ready"
  fi
  exit 0
fi
if [[ "$1" == -c ]]; then [[ -f "$0.ready" ]]; exit; fi
exit 1
''', encoding='utf-8', newline='\n')
    # This Windows test volume does not implement MSYS chmod; Linux uses native install.
    windows_install = '''
install() {
  if [[ "$1" == -d ]]; then shift; if [[ "$1" == -m ]]; then shift 2; fi; mkdir -p -- "$1";
  else shift 2; cp -- "$1" "$2"; fi
}
''' if os.name == 'nt' else ''
    script = windows_install + function('database_runtime_ready') + function('install_database_dependencies') + '''
selected_python() {
  if [[ "$1" == -m && "$2" == venv ]]; then
    printf 'VENV %s\\n' "$*" >>"$CALLS"
    install -d "$4/bin"
    install -m 755 "$FAKE" "$4/bin/python"
    return 0
  fi
  return 1
}
assert_trusted_directory_chain() { return 0; }
validate_existing_control_file() { return 0; }
upsert_env_assignment() { printf 'ENV %s=%s\\n' "$1" "$2"; }
install_database_dependencies
printf 'FINAL_PYTHON=%s\\nDATABASE=%s\\n' "$PYTHON_BIN" "$DATABASE_SETUP_RESULT"
'''
    calls = tmp_path / 'calls'
    result = bash_run(script, APP_HOME=app.as_posix(), PYTHON_BIN='selected_python', FAKE=fake.as_posix(),
                      CALLS=calls.as_posix(), FAIL=str(fail).lower(), SETUP_DATABASE='no' if skip else 'auto')
    return result, app, calls.read_text() if calls.exists() else ''


def test_dependencies_are_installed_in_private_venv_before_runtime_switch(tmp_path):
    result, app, calls = prepare(tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'VENV -m venv --copies' in calls
    assert '-r ' + app.as_posix() + '/requirements-ssh.txt' in calls
    assert '-r ' + app.as_posix() + '/ui/requirements-database.txt' in calls
    assert '--break-system-packages' not in calls
    assert 'ENV DEPLOY_AGENT_PYTHON_BIN=' + app.as_posix() + '-runtime/bin/python' in result.stdout
    assert 'FINAL_PYTHON=' + app.as_posix() + '-runtime/bin/python' in result.stdout
    assert not (app / '.database-runtime').exists()


def test_download_failure_keeps_original_runtime_and_files_for_retry(tmp_path):
    result, app, calls = prepare(tmp_path, fail=True)
    assert result.returncode == 0, result.stderr
    assert 'PIP ' in calls and 'ENV DEPLOY_AGENT_PYTHON_BIN=' not in result.stdout
    assert 'FINAL_PYTHON=selected_python' in result.stdout
    assert (app.parent / 'panel-runtime/.mini-deploy-runtime').is_file()


def test_explicit_skip_does_not_create_runtime_or_install_packages(tmp_path):
    result, app, calls = prepare(tmp_path, skip=True)
    assert result.returncode == 0, result.stderr
    assert not calls and not (app.parent / 'panel-runtime').exists()
    assert 'DATABASE=skipped' in result.stdout


def test_unmarked_environment_is_never_adopted_or_overwritten(tmp_path):
    result, app, calls = prepare(tmp_path, unmarked=True)
    assert result.returncode == 0, result.stderr
    assert not calls and 'ENV DEPLOY_AGENT_PYTHON_BIN=' not in result.stdout
    assert (app.parent / 'panel-runtime/keep.txt').read_text() == 'preserve'
