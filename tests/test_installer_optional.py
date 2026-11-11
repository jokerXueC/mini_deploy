"""Optional package failures cannot be mistaken for usable services."""
import pytest

from test_installer_operations import bash_run, function


@pytest.mark.parametrize('component', ['nginx', 'certbot'])
@pytest.mark.parametrize('manager', ['apt-get', 'dnf', 'yum'])
def test_optional_package_failure_stops_component_even_when_called_in_conditional(component, manager):
    name = f'install_{component}_if_missing'
    result = bash_run(function(name) + r'''
command() {
  if [[ "$1" == -v ]]; then [[ "$2" == "$MANAGER" ]]; return; fi
  builtin command "$@"
}
is_en() { return 0; }
ask_yes_no() { return 0; }
nginx_install_is_preapproved() { return 0; }
apt-get() { echo package-failed; return 1; }
dnf() { echo package-failed; return 1; }
yum() { echo package-failed; return 1; }
systemctl() { echo unexpected-service-change; }
''' + f'\nif {name}; then echo incorrect-success; else echo component-unavailable; fi',
        MANAGER=manager, SETUP_HTTPS='yes')
    assert result.returncode == 0, result.stderr
    assert 'component-unavailable' in result.stdout
    assert 'incorrect-success' not in result.stdout
    assert 'unexpected-service-change' not in result.stdout


def test_preapproved_https_does_not_ask_again_before_installing_certbot():
    result = bash_run(function('install_certbot_if_missing') + r'''
ready=false
command() {
  if [[ "$1" == -v && "$2" == certbot ]]; then [[ "$ready" == true ]]; return; fi
  builtin command "$@"
}
is_en() { return 0; }
ask_yes_no() { echo unexpected-prompt; return 1; }
apt-get() { echo "PACKAGE $*"; ready=true; }
install_certbot_if_missing
''', SETUP_HTTPS='yes')
    assert result.returncode == 0, result.stderr
    assert 'PACKAGE install -y certbot python3-certbot-nginx' in result.stdout
    assert 'unexpected-prompt' not in result.stdout


def test_disabled_https_never_probes_or_installs_certbot():
    result = bash_run(function('setup_nginx') + r'''
is_en() { return 0; }
install_nginx_if_missing() { return 0; }
write_nginx_config() { return 0; }
nginx() { return 0; }
upsert_env_assignment() { return 0; }
systemctl() { return 0; }
install_certbot_if_missing() { echo unexpected-certbot; return 1; }
setup_nginx example.test
''', SETUP_HTTPS='no', SERVICE_NAME='test-agent')
    assert result.returncode == 0, result.stderr
    assert 'unexpected-certbot' not in result.stdout
