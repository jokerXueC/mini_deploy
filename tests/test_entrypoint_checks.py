import json

import pytest

import entrypoint_checks as entry


@pytest.mark.parametrize(('value', 'expected'), [
    ('80:80', {'host': '0.0.0.0', 'port': 80, 'protocol': 'tcp'}),
    ('127.0.0.1:8080:80', {'host': '127.0.0.1', 'port': 8080, 'protocol': 'tcp'}),
    ('[::1]:443:443', {'host': '::1', 'port': 443, 'protocol': 'tcp'}),
    ({'target': 443, 'published': '8443', 'protocol': 'udp'}, {'host': '0.0.0.0', 'port': 8443, 'protocol': 'udp'}),
    ('80', {}), (80, {}), ('${PORT:-80}:80', None), ('8000-8010:80-90', None),
])
def test_bindings(value, expected):
    assert entry.parse_binding(value) == expected


def test_manifest_and_owner_hints_are_read_only(monkeypatch, tmp_path):
    content = 'services:\n  web:\n    image: nginx:alpine\n    ports: ["80:80", "443:443"]\n'
    path = tmp_path / 'compose.yaml'
    path.write_text(content)
    monkeypatch.setattr(entry.shutil, 'which', lambda name: name)
    commands = []

    def read(command):
        commands.append(command)
        if command[0] == 'docker':
            return json.dumps({'Names': 'mini-deploy-nginx', 'Ports': '0.0.0.0:80->80/tcp'})
        return 'LISTEN 0 511 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=15,fd=7))'

    monkeypatch.setattr(entry, '_read', read)
    report = entry.inspect_directory(tmp_path)
    advice = '\n'.join(entry.entry_advice(report))
    assert 'mini-deploy-nginx' in advice and '进程 nginx' in advice
    assert '可能是本项目' in advice and '无需再安装' in advice
    assert path.read_text() == content
    assert [command[:2] for command in commands] == [['docker', 'ps'], ['ss', '-H']]


def test_internal_only_does_not_probe_host(monkeypatch):
    report = entry.inspect_files({'compose.yml': 'services: {web: {image: nginx, ports: [80]}}'})
    monkeypatch.setattr(entry, '_read', lambda *_: pytest.fail('must not probe'))
    assert not report['bindings']
    assert not any('已监听' in item for item in entry.entry_advice(report))


@pytest.mark.parametrize('text', ['', 'services: [bad]', 'services: {web: &a {image: nginx}, other: *a}',
                                  '!!python/object/apply:os.system [whoami]', 'a' * (entry.LIMIT + 1)],
                         ids=['empty', 'invalid', 'alias', 'unsafe-tag', 'oversized'])
def test_unsafe_or_unsupported_yaml_is_not_reported_as_clear(text):
    report = entry.inspect_files({'compose.yml': text})
    assert report['warnings']


def test_override_host_network_and_unknown_variables_are_visible():
    report = entry.inspect_files({
        'compose.yml': 'include: other.yml\nservices: {web: {network_mode: host, ports: ["${PORT}:80"]}}',
        'compose.override.yml': 'services: {web: {profiles: [edge], ports: ["8080:80"]}}',
    })
    advice = '\n'.join(report['warnings'])
    assert all(word in advice for word in ('未合并', '主机网络', '变量', 'include', 'profiles'))


def test_probe_failure_never_means_free(monkeypatch):
    report = entry.inspect_files({'compose.yml': 'services: {web: {ports: ["80:80"]}}'})
    monkeypatch.setattr(entry.shutil, 'which', lambda name: name)
    monkeypatch.setattr(entry, '_read', lambda command: (_ for _ in ()).throw(OSError()))
    assert any('不代表端口空闲' in text for text in entry.entry_advice(report))
