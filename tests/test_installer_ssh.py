"""Dependency setup is idempotent and cannot break non-SSH panel features."""
from test_installer_operations import bash_run, function


def run(body):
    return bash_run(function("install_ssh_dependency") + '''
PYTHON_BIN=python_mock
SERVICE_NAME=mini-deploy-agent
is_en() { return 1; }
''' + body + "\ninstall_ssh_dependency\necho finished\n")


def test_existing_paramiko_skips_package_manager():
    result = run('''
python_mock() { return 0; }
apt-get() { echo unexpected-package-call; return 1; }
''')
    assert result.returncode == 0
    assert "unexpected-package-call" not in result.stdout
    assert "finished" in result.stdout


def test_missing_dependency_installs_distribution_package():
    result = run('''
ssh_package_ready=false
python_mock() { [[ "$ssh_package_ready" == true ]]; }
apt-get() {
  echo "$*"
  if [[ "$1" == install ]]; then ssh_package_ready=true; fi
  return 0
}
''')
    assert result.returncode == 0
    assert "install -y python3-paramiko" in result.stdout
    assert "依赖暂不可用" not in result.stdout


def test_network_failure_preserves_non_ssh_installation_flow():
    result = run('''
python_mock() { return 1; }
apt-get() { return 1; }
''')
    assert result.returncode == 0
    assert "其他面板功能可正常使用" in result.stdout
    assert "finished" in result.stdout
