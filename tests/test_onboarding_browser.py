"""Optional browser regression checks: pytest tests/test_onboarding_browser.py."""
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import agent
import project_guidance

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def browser_page():
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def do_GET(self):
            if self.path == '/ui':
                self.path = '/ui/index.html'
            super().do_GET()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except playwright.Error:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            pytest.skip('Playwright Chromium is not installed')
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        state = {'projects': [], 'posts': [], 'blocked': False, 'failed_bootstrap': False, 'failed_connection': False, 'candidates': None, 'files': None}
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def api(route):
            path = route.request.url.split(server_url, 1)[-1].split('?')[0]
            if path.startswith('/ui'):
                route.continue_()
                return
            payload = json.loads(route.request.post_data or '{}')
            project = payload.get('project', {})
            if route.request.method == 'POST':
                state['posts'].append((path, payload))
            result = {}
            if path == '/status':
                result = dict(projects=state['projects'], state={}, system={}, events=[], alerts=[], csrf_token='test')
            elif path == '/projects-config':
                result = dict(projects=state['projects'], notifications={})
            elif path == '/projects-config/inspect':
                result = dict(ok=True, branch=project.get('branch') or 'master', branches=['master', 'release'],
                              existing_script='', warnings=[], candidates=[dict(template='python', entry='main:app')],
                              provider=project_guidance.provider_info(project['repo'], project.get('repository_provider', 'auto')))
                if state['candidates'] is not None:
                    result['candidates'] = state['candidates']
                if state['files'] is not None:
                    result.update(project_guidance.detect(state['files']))
                if state['failed_connection']:
                    result.update(ok=False, diagnosis=[dict(title='仓库无权限', advice='请配置部署密钥')])
            elif path == '/projects-config/preview':
                try:
                    resolved = agent._project_from_form(project)
                except ValueError as exc:
                    route.fulfill(status=400, json={'detail': str(exc)})
                    return
                result = {'start_command': resolved.start_command, 'script': resolved.script,
                          'files': [dict(kind='deploy.sh', path=project['script'], content='# preview', exists=False)]}
            elif path in ('/projects-config/bootstrap', '/projects-config/save'):
                state['projects'] = [{**project, 'webhook_secret': 'test'}]
                result = dict(projects=state['projects'], bootstrap=dict(ok=not state['failed_bootstrap'], results=[]))
            elif path == '/projects-config/doctor':
                result = {'checks': [dict(title='Python', message='运行环境', ok=not state['blocked'], level='fail')]}
            elif path == '/preflight':
                result = {'items': [dict(title='Project is disabled', detail='', level='critical')]}
            elif path == '/nginx-requests':
                result = state.get('nginx_requests', {'mode': 'none', 'enabled': False, 'records': [], 'notice': 'No Nginx'})
            elif path == '/caddy-requests':
                result = state.get('caddy_requests', {'mode': 'caddy', 'container': '', 'candidates': [],
                                                      'records': [], 'notice': 'No Caddy logs'})
            route.fulfill(json=result)

        server_url = f'http://127.0.0.1:{server.server_port}'
        page.route(server_url + '/**', api)
        page.goto(server_url + '/ui')
        page.locator('#addProjectBtn').click()
        page.locator('#wizardSituation').select_option('new', force=True)
        try:
            yield page, state
            assert not errors, errors
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


def connect_and_review(page):
    page.locator('#projectRepoInput').fill('https://example.test/demo.git')
    page.locator('#projectRepoInput').press('Enter')
    page.wait_for_selector('#wizardConfigure', state='visible')
    assert page.locator('#projectNameInput').input_value() == 'demo'
    assert page.locator('#projectBranchInput').input_value() == 'master'
    assert page.locator('#wizardEntryPath').input_value() == 'main.py'
    assert page.locator('#wizardEntryObject').input_value() == 'app'
    assert page.locator('#projectStartCommandInput').is_hidden()
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardReview', state='visible')


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('editing', [False, True])
def test_repository_modal_backdrop_does_not_close_or_discard_fields(browser_page, width, editing):
    page, _state = browser_page
    page.set_viewport_size({'width': width, 'height': 844})
    if editing:
        page.locator('#closeProjectModalBtn').click()
        page.evaluate("openProjectModal({key:'existing',name:'Existing',repo:'https://example.test/a.git',template:'custom',enabled:true})")
    page.locator('#projectRepoInput').fill('https://example.test/keep-my-input.git')
    assert page.evaluate("document.elementFromPoint(4, 4).id") == 'projectModal'
    page.mouse.click(4, 4)
    assert page.locator('#projectModal').is_visible()
    assert page.locator('#projectRepoInput').input_value() == 'https://example.test/keep-my-input.git'
    page.locator('#projectModalTitle').click()
    assert page.locator('#projectModal').is_visible()
    page.locator('#closeProjectModalBtn').click()
    assert page.locator('#projectModal').is_hidden()


def test_first_deploy_requires_confirmation_and_ready_environment(browser_page):
    page, state = browser_page
    assert page.locator('#projectRepoInput').is_visible()
    assert page.locator('#projectKeyInput').is_hidden()
    assert page.locator('#projectWebhookUrl').is_hidden()
    connect_and_review(page)
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardError', state='visible')
    assert not any(path == '/projects-config/bootstrap' for path, _ in state['posts'])
    page.locator('#projectConfigConfirmed').check()
    state['blocked'] = True
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardDeploy', state='visible')
    page.locator('#wizardDeploy').click()
    page.wait_for_function('!ProjectWizard.busy()')
    assert not any(path == '/redeploy' for path, _ in state['posts'])
    state['blocked'] = False
    page.locator('#wizardDeploy').click()
    page.wait_for_selector('#projectModal', state='hidden')
    assert sum(path == '/redeploy' for path, _ in state['posts']) == 1
    saved = next(data for path, data in state['posts'] if path == '/projects-config/save')
    assert saved['project']['enabled'] is True
    assert saved['original_key'] == 'demo'
    assert saved['project']['entry_kind'] == 'fastapi'
    assert 'main:app' in saved['project']['start_command']


def test_connection_retry_and_failed_initialization_reuse_saved_project(browser_page):
    page, state = browser_page
    state['failed_connection'] = True
    page.locator('#projectRepoInput').fill('https://example.test/demo.git')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardError', state='visible')
    assert page.locator('#wizardConfigure').is_hidden()
    state['failed_connection'] = False
    connect_and_review(page)
    page.locator('#projectConfigConfirmed').check()
    state['failed_bootstrap'] = True
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardError', state='visible')
    assert not any(path == '/redeploy' for path, _ in state['posts'])
    state['failed_bootstrap'] = False
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardDeploy', state='visible')
    attempts = [body for path, body in state['posts'] if path == '/projects-config/bootstrap']
    assert len(attempts) == 2
    assert attempts[-1]['original_key'] == 'demo'


def test_mobile_layout_and_existing_project_editing(browser_page, tmp_path):
    page, _state = browser_page
    page.set_viewport_size({'width': 390, 'height': 844})
    connect_and_review(page)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'wizard-mobile.png'))
    page.locator('#wizardBack').click()
    page.locator('#projectServicePortInput').fill('0')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardError', state='visible')
    assert page.locator('#wizardReview').is_hidden()
    page.locator('#closeProjectModalBtn').click()
    page.evaluate("openProjectModal({key:'existing',name:'Existing',repo:'https://example.test/a.git',template:'docker',enabled:true})")
    assert page.locator('#projectWizard').is_hidden()
    assert page.locator('#projectKeyInput').is_visible()
    assert not page.locator('#projectStartCommandInput').evaluate('(el) => el.required')


def test_multiple_entries_require_choice_and_plain_python_generates_command(browser_page):
    page, state = browser_page
    state['candidates'] = [dict(template='python', entries=[
        dict(kind='fastapi', path='api/main.py', object='api'),
        dict(kind='python', path='worker.py', object='')])]
    page.locator('#projectRepoInput').fill('https://example.test/demo.git')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardConfigure', state='visible')
    assert page.locator('#wizardEntryPath').input_value() == ''
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardError', state='visible')
    page.locator('#wizardEntryChoice').select_option('1', force=True)
    assert page.locator('#wizardEntryPath').input_value() == 'worker.py'
    assert page.locator('#wizardEntryObject').is_hidden()
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardReview', state='visible')
    assert '.venv/bin/python' in page.locator('#projectStartCommandInput').input_value()
    assert 'uvicorn' not in page.locator('#projectStartCommandInput').input_value()


def test_custom_command_and_invalid_relative_entry(browser_page):
    page, _state = browser_page
    connect_and_review(page)
    page.locator('#wizardBack').click()
    page.locator('#wizardEntryPath').fill('../main.py')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardError', state='visible')
    assert '相对路径' in page.locator('#wizardError').inner_text()
    page.locator('#wizardConfigure details summary').click()
    page.locator('#wizardCustomCommand').check()
    page.locator('#projectStartCommandInput').fill('/usr/bin/custom-runner')
    assert page.locator('#wizardEntryPath').is_hidden()
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardReview', state='visible')
    preview = [body for path, body in _state['posts'] if path == '/projects-config/preview'][-1]
    assert preview['project']['entry_kind'] == ''
    assert preview['project']['start_command'] == '/usr/bin/custom-runner'


def test_dockerfile_wizard_autofills_port_and_keeps_secrets_out_of_preview(browser_page):
    page, state = browser_page
    state['files'] = {'Dockerfile': 'FROM python:3.12\nEXPOSE 8000\nCMD python main.py',
                      '.env.example': 'DATABASE_URL=example-not-used'}
    page.locator('#projectRepoInput').fill('https://example.test/demo.git')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardConfigure', state='visible')
    assert page.locator('#wizardEntryFields').is_hidden()
    assert page.locator('#wizardDockerContainerPort').input_value() == '8000'
    assert page.locator('[data-env-value]').input_value() == ''
    page.locator('[data-env-value]').fill('postgres://secret@db/app')
    page.locator('#wizardDockerPersist').check()
    page.locator('#wizardDockerVolumes').fill('/app/uploads')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardReview', state='visible')
    assert '8080' in page.locator('#wizardSummary').inner_text()
    assert '/app/uploads' in page.locator('#wizardSummary').inner_text()
    assert 'postgres://secret' not in page.locator('#wizardReview').inner_text()
    preview = next(body for path, body in state['posts'] if path == '/projects-config/preview')
    assert 'postgres://secret' not in json.dumps(preview)
    assert preview['project']['docker_config']['environment'] == ['DATABASE_URL']
    page.locator('#projectConfigConfirmed').check()
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardDeploy', state='visible')
    bootstrap = next(body for path, body in state['posts'] if path == '/projects-config/bootstrap')
    assert bootstrap['options']['environment']['DATABASE_URL'] == 'postgres://secret@db/app'
    assert 'postgres://secret' not in json.dumps(bootstrap['project'])
    page.locator('#wizardWebhookSetup summary').click()
    assert '/webhook' in page.locator('#wizardWebhookUrl').inner_text()
    page.locator('#wizardDeploy').click()
    page.wait_for_selector('#projectModal', state='hidden')
    saved = next(body for path, body in state['posts'] if path == '/projects-config/save')
    assert 'postgres://secret' not in json.dumps(saved)
    assert page.locator('[data-env-value]').count() == 0


def test_compose_required_variables_and_mobile_layout(browser_page, tmp_path):
    page, state = browser_page
    state['files'] = {'compose.yaml': 'services:\n  api:\n    image: ${IMAGE:?required}\n'}
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('#projectRepoInput').fill('https://example.test/demo.git')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardConfigure', state='visible')
    assert page.locator('#wizardDockerBuildFields').is_hidden()
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardError', state='visible')
    assert page.locator('#wizardReview').is_hidden()
    page.locator('[data-env-value]').fill('nginx:stable-alpine')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'docker-compose-mobile.png'))
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardReview', state='visible')
    preview = next(body for path, body in state['posts'] if path == '/projects-config/preview')
    assert preview['project']['docker_config']['mode'] == 'compose'


def test_ambiguous_docker_port_requires_input_and_retry_can_change_port(browser_page):
    page, state = browser_page
    state['files'] = {'Dockerfile': 'FROM python:3\nCMD python main.py\n'}
    page.locator('#projectRepoInput').fill('https://example.test/demo.git')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardConfigure', state='visible')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardError', state='visible')
    page.locator('#wizardDockerContainerPort').fill('8000')
    page.locator('#wizardDockerPublishedPort').fill('6868')
    page.locator('#wizardNext').click()
    page.wait_for_function('!ProjectWizard.busy()')
    assert '6868' in page.locator('#wizardError').inner_text()
    page.locator('#wizardDockerPublishedPort').fill('8080')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardReview', state='visible')
    page.locator('#projectConfigConfirmed').check()
    state['blocked'] = True
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardDeploy', state='visible')
    page.locator('#wizardBack').click()
    page.locator('#wizardDockerPublishedPort').fill('8081')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardReview', state='visible')
    page.locator('#projectConfigConfirmed').check()
    page.locator('#wizardNext').click()
    page.wait_for_function('!ProjectWizard.busy()')
    bodies = [body for path, body in state['posts'] if path == '/projects-config/bootstrap']
    assert bodies[-1]['original_key'] == 'demo'
    assert bodies[-1]['project']['docker_config']['published_port'] == 8081


def test_switching_runtime_ignores_invalid_hidden_docker_fields(browser_page):
    page, state = browser_page
    state['files'] = {'Dockerfile': 'FROM python:3\nEXPOSE 8000\nCMD python main.py',
                      'requirements.txt': 'fastapi', 'main.py': 'from fastapi import FastAPI\napp = FastAPI()'}
    page.locator('#projectRepoInput').fill('https://example.test/demo.git')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardConfigure', state='visible')
    page.locator('#wizardDockerContainerPort').fill('0')
    page.locator('#projectTemplateInput').select_option('python', force=True)
    assert page.locator('#wizardDockerFields').is_hidden()
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardReview', state='visible')
    preview = next(body for path, body in state['posts'] if path == '/projects-config/preview')
    assert preview['project']['docker_config'] == {}
    assert preview['project']['entry_kind'] == 'fastapi'


def test_unknown_repository_can_use_commands_without_script_or_service(browser_page, tmp_path):
    page, state = browser_page
    state['files'] = {'README.md': ''}
    page.locator('#projectRepoInput').fill('https://git.example.test/team/demo.git')
    page.locator('#projectProviderInput').select_option('gitea', force=True)
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardConfigure', state='visible')
    assert page.locator('#wizardDeployMethod').input_value() == ''
    assert 'Gitea' in page.locator('#wizardPlatformAdvice').inner_text()
    page.locator('#wizardDeployMethod').select_option('commands', force=True)
    assert page.locator('#projectScriptInput').is_hidden()
    assert page.locator('#projectTemplateInput').is_hidden()
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardError', state='visible')
    assert page.locator('#wizardReview').is_hidden()
    page.locator('#projectBuildStepInput').fill('npm ci\nnpm run build')
    page.locator('#projectRestartStepInput').fill('systemctl restart my-real-service')
    page.screenshot(path=str(tmp_path / 'commands-desktop.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'commands-mobile.png'))
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardReview', state='visible')
    assert 'systemctl restart my-real-service' in page.locator('#wizardSummary').inner_text()
    assert 'npm ci\nnpm run build' in page.locator('#wizardSummary').inner_text()
    body = [body for path, body in state['posts'] if path == '/projects-config/preview'][-1]
    assert body['project']['deployment_plan'] == dict(situation='new', method='commands', build='npm ci\nnpm run build', restart='systemctl restart my-real-service')
    assert body['project']['repository_provider'] == 'gitea'
    assert body['project']['trigger_mode'] == 'manual'
    page.locator('#projectConfigConfirmed').check()
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardDeploy', state='visible')
    page.locator('#wizardWebhookSetup summary').click()
    assert 'Gitea' in page.locator('#wizardWebhookGuide').inner_text()
    assert '不会触发' in page.locator('#wizardWebhookModeHint').inner_text()


def test_existing_service_can_finish_without_forced_update(browser_page):
    page, state = browser_page
    page.locator('#wizardSituation').select_option('existing', force=True)
    page.locator('#wizardExistingDirectory').fill('/opt/already-running')
    page.locator('#projectRepoInput').fill('https://example.test/demo.git')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardConfigure', state='visible')
    assert page.locator('#wizardDeployMethod').input_value() == 'commands'
    assert page.locator('#projectWorkdirInput').input_value() == '/opt/already-running'
    assert page.locator('#wizardEntryFields').is_hidden()
    page.locator('#projectRestartStepInput').fill('systemctl restart existing-api')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardReview', state='visible')
    assert '不拉取' in page.locator('#wizardReviewHint').inner_text()
    page.locator('#projectConfigConfirmed').check()
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardDeploy', state='visible')
    assert page.locator('#wizardDeploy').inner_text() == '执行一次更新'
    page.locator('#wizardFinish').click()
    page.wait_for_selector('#projectModal', state='hidden')
    assert not any(path == '/redeploy' for path, _ in state['posts'])
    body = next(body for path, body in state['posts'] if path == '/projects-config/bootstrap')
    assert body['project']['deployment_plan']['situation'] == 'existing'
    assert body['options']['write_service'] is False
    saved = next(body for path, body in state['posts'] if path == '/projects-config/save')
    assert saved['project']['enabled'] is True
    assert saved['project']['trigger_mode'] == 'manual'


def test_unsure_or_missing_situation_cannot_prepare(browser_page):
    page, state = browser_page
    page.locator('#wizardSituation').select_option('', force=True)
    page.locator('#projectRepoInput').fill('https://example.test/demo.git')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardError', state='visible')
    assert not state['posts']
    page.locator('#wizardSituation').select_option('unsure', force=True)
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardConfigure', state='visible')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardError', state='visible')
    assert '返回第一步' in page.locator('#wizardError').inner_text()
    assert not any(path == '/projects-config/bootstrap' for path, _ in state['posts'])


def test_existing_compose_avoids_new_container_plan_and_supports_explicit_script(browser_page):
    page, state = browser_page
    state['files'] = {'compose.yaml': 'services:\n  api:\n    image: busybox\n', 'Dockerfile': 'FROM busybox\n'}
    page.locator('#wizardSituation').select_option('existing', force=True)
    page.locator('#wizardExistingDirectory').fill('/srv/existing-compose')
    page.locator('#projectRepoInput').fill('https://example.test/demo.git')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardConfigure', state='visible')
    assert page.locator('#projectTemplateInput').input_value() == 'docker'
    assert not page.locator('#wizardDockerChoice option[value="dockerfile"]').count()
    page.locator('#wizardDeployMethod').select_option('script', force=True)
    assert page.locator('#projectScriptInput').input_value() == ''
    page.locator('#projectScriptInput').fill('ops/update.sh')
    page.locator('#wizardNext').click()
    page.wait_for_selector('#wizardReview', state='visible')
    body = [body for path, body in state['posts'] if path == '/projects-config/preview'][-1]
    assert body['project']['deployment_plan']['method'] == 'script'
    assert body['project']['script'] == 'ops/update.sh'
    assert body['project']['docker_config'] == {}


@pytest.mark.parametrize('width', [1440, 390])
def test_nginx_request_view_filters_and_escapes(browser_page, width):
    page, state = browser_page
    page.locator('#closeProjectModalBtn').click()
    page.set_viewport_size({'width': width, 'height': 844})
    state['nginx_requests'] = {
        'mode': 'docker', 'container': 'edge', 'enabled': True, 'notice': 'Only new requests',
        'records': [
            {'at': '2026-09-30T12:34:56+08:00', 'host': 'example.test', 'method': 'GET',
             'path': '/safe<script>alert(1)</script>', 'status': 200, 'duration_ms': 12, 'upstream_ms': 10},
            {'at': '2026-09-30T12:35:00+08:00', 'host': 'example.test', 'method': 'POST',
             'path': '/failed', 'status': 500, 'duration_ms': 33, 'upstream_ms': None},
        ],
    }
    page.locator('#requestsViewTab').click()
    page.wait_for_selector('.request-row:not(.request-columns)')
    assert page.locator('.request-row:not(.request-columns)').count() == 2
    assert page.locator('#requestRows script').count() == 0
    page.locator('#requestStatus').select_option('error')
    assert page.locator('.request-row:not(.request-columns)').count() == 1
    assert '/failed' in page.locator('#requestRows').inner_text()
    page.locator('#requestSearch').fill('missing')
    assert page.locator('.request-row:not(.request-columns)').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


@pytest.mark.parametrize('width', [1440, 390])
def test_caddy_request_view_auto_selects_and_filters(browser_page, width):
    page, state = browser_page
    page.locator('#closeProjectModalBtn').click()
    page.set_viewport_size({'width': width, 'height': 844})
    state['caddy_requests'] = {'mode': 'caddy', 'container': 'aimore-caddy-1',
                               'candidates': ['aimore-caddy-1'], 'notice': 'Active', 'records': [
                                   {'at': '2026-09-30T06:34:56Z', 'host': 'example.test', 'method': 'GET',
                                    'path': '/ok<script>alert(1)</script>', 'status': 200,
                                    'duration_ms': 12, 'upstream_ms': None}]}
    page.locator('#requestsViewTab').click()
    page.wait_for_selector('.request-row:not(.request-columns)')
    assert page.locator('#requestBackend').input_value() == 'caddy'
    assert page.locator('#requestContainer').input_value() == 'aimore-caddy-1'
    assert page.locator('#requestEnable').is_hidden()
    assert page.locator('#requestRows script').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
