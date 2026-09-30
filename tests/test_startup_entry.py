import pytest

import agent
import project_guidance as guidance


@pytest.mark.parametrize(('kind', 'entry', 'obj', 'expected'), [
    ('fastapi', 'app/main.py', 'api', 'app.main:api --host 127.0.0.1 --port 8123'),
    ('python', 'scripts/server.py', '', '/.venv/bin/python" "/srv/demo/scripts/server.py"'),
])
def test_python_entry_generates_command_and_round_trips(kind, entry, obj, expected):
    project = agent._project_from_form(dict(key='demo', template='python', workdir='/srv/demo',
        service_port=8123, entry_kind=kind, entry_path=entry, entry_object=obj))
    assert expected in project.start_command
    reloaded = agent._project_from_config(agent._project_to_config(project), 'demo')
    assert reloaded.entry_path == entry
    assert agent._default_start_command(reloaded) == project.start_command
    assert project.start_command in agent._systemd_service_text(reloaded)


@pytest.mark.parametrize(('template', 'kind', 'entry', 'obj'), [
    ('python', 'fastapi', '../main.py', 'app'),
    ('python', 'fastapi', '/root/main.py', 'app'),
    ('python', 'fastapi', 'main.py', 'app;id'),
    ('python', 'fastapi', 'my-app/main.py', 'app'),
    ('python', 'python', 'main.py\nreboot', ''),
    ('go', 'go', 'main.go', ''),
    ('go', 'go', '-exec', ''),
    ('java', 'java', 'target/*.jar', ''),
    ('java', 'python', 'main.py', ''),
])
def test_invalid_entries_do_not_generate_commands(template, kind, entry, obj):
    with pytest.raises(ValueError):
        guidance.entry_command(template, kind, entry, obj, '/srv/demo', 8000)


def test_go_directory_affects_real_build():
    project = agent._project_from_form(dict(key='api', template='go', workdir='/srv/api',
                                          entry_kind='go', entry_path='cmd/api'))
    assert 'go build -o bin/app ./cmd/api' in agent._deploy_script_text(project)
    assert project.start_command == '"/srv/api/bin/app"'


def test_java_selected_artifact_is_copied_after_build():
    project = agent._project_from_form(dict(key='api', template='java', workdir='/srv/api',
                                          entry_kind='java', entry_path='api/target/server.jar'))
    script = agent._deploy_script_text(project)
    assert 'test -f api/target/server.jar' in script
    assert 'cp -- api/target/server.jar target/deploy/app.jar' in script
    assert 'find "$jar_dir"' not in script


def test_editing_full_command_disables_structured_generation():
    project = agent._project_from_form(dict(key='api', template='python', workdir='/srv/api',
                                          entry_kind='fastapi', entry_path='main.py', entry_object='app'))
    changed = agent._project_from_form({'start_command': '/usr/bin/custom-runner'}, project)
    assert changed.entry_kind == ''
    assert agent._default_start_command(changed) == '/usr/bin/custom-runner'
    unchanged = agent._project_from_form({'name': 'New name'}, project)
    assert unchanged.entry_kind == 'fastapi'


def test_detection_reports_aliases_and_multiple_objects_without_executing():
    result = guidance.detect({'requirements.txt': '', 'main.py':
        'from fastapi import FastAPI as API\nfirst = API()\nsecond = API()\nraise RuntimeError()',
        'server.py': 'print("hello")', 'app/main.py': 'import fastapi as fa\napp = fa.FastAPI()'})
    choices = result['candidates'][0]['entries']
    assert {'kind': 'fastapi', 'path': 'main.py', 'object': 'second'} in choices
    assert {'kind': 'fastapi', 'path': 'app/main.py', 'object': 'app'} in choices
    assert {'kind': 'python', 'path': 'server.py', 'object': ''} in choices
    assert result['candidates'][0]['entry'] == ''
