"""File UI through authenticated HTTP and actual loopback SSH/SFTP."""
import time

import agent
from ssh_sftp_peer import enable_sftp
from test_ssh_browser import fill, page as page, playwright
from test_ssh_terminal import http_server as http_server, manager as manager, peer as peer, request, runtime as runtime


def prepare(page, http_server, manager, peer, tmp_path):
    root = tmp_path / 'remote'
    root.mkdir()
    (root / 'app.conf').write_text('port=8080\n', encoding='utf-8')
    (root / '.hidden').write_text('hidden')
    (root / 'archive.bin').write_bytes(bytes(range(256)) * 2000)
    (root / "a&b'.txt").write_text('safe')
    page.goto(http_server.origin + '/ui?view=terminal')
    fill(page, peer)
    page.locator('#sshConnect').click()
    page.locator('#sshTrustCheck').check()
    page.locator('#sshTrustConfirm').click()
    playwright.expect(page.locator('#sshState')).to_have_text('已连接')
    enable_sftp(peer, root)
    page.locator('#sshFilesOpen').click()
    playwright.expect(page.locator('#sfPath')).to_have_value('/')
    return root


def row(page, name):
    return page.locator('.sf-row').filter(has=page.locator('strong', has_text=name))


def test_edit_create_download_upload_and_desktop_layout(page, http_server, manager, peer, tmp_path):
    root = prepare(page, http_server, manager, peer, tmp_path)
    playwright.expect(row(page, '.hidden')).to_have_count(0)
    page.locator('#sfHidden').check()
    playwright.expect(row(page, '.hidden')).to_have_count(1)
    assert page.locator('#sfList img').count() == 4  # Names remain escaped text.
    row(page, 'app.conf').click()
    playwright.expect(page.locator('#sfEditor')).to_have_value('port=8080\n')
    page.locator('#sfEditor').fill('port=8081\n')
    page.locator('#sfSave').click()
    playwright.expect(page.locator('#sfDiff')).to_contain_text('-port=8080')
    assert (root / 'app.conf').read_text() == 'port=8080\n'
    page.locator('#sfReviewConfirm').click()
    playwright.expect(page.locator('#sfReviewDialog')).not_to_be_visible()
    assert (root / 'app.conf').read_text() == 'port=8081\n'
    assert len(list(root.glob('app.conf.mini-deploy-backup.*'))) == 1
    page.locator('#sfDivider').focus()
    page.keyboard.press('ArrowRight')
    playwright.expect(page.locator('#sfDivider')).to_have_attribute('aria-valuenow', '49')
    page.screenshot(path=str(tmp_path / 'files-desktop.png'), full_page=True)
    page.locator('#sfNewFolder').click()
    page.locator('#sfCreateName').fill('logs')
    page.locator('#sfCreateConfirm').click()
    playwright.expect(row(page, 'logs')).to_have_count(1)
    assert (root / 'logs').is_dir()
    page.locator('#sfNewFile').click()
    page.locator('#sfCreateName').fill('empty.txt')
    page.locator('#sfCreateConfirm').click()
    page.locator('#sfSave').click()
    page.locator('#sfReviewConfirm').click()
    playwright.expect(page.locator('#sfReviewDialog')).not_to_be_visible()
    assert (root / 'empty.txt').read_bytes() == b''
    # A 512 KB binary file exercises real authenticated chunked HTTP transfer.
    row(page, 'archive.bin').click()
    playwright.expect(page.locator('#sfFeedback')).to_contain_text('二进制')
    with page.expect_download() as download:
        page.locator('#sfDownload').click()
    assert download.value.path().read_bytes() == (root / 'archive.bin').read_bytes()
    playwright.expect(page.locator('#sfUpload')).to_be_enabled()
    page.on('dialog', lambda dialog: dialog.accept())
    page.locator('#sfUploadInput').set_input_files({'name': 'upload.bin', 'mimeType': 'application/octet-stream', 'buffer': b'a\0b' * 100000})
    playwright.expect(row(page, 'upload.bin')).to_have_count(1, timeout=15000)
    assert (root / 'upload.bin').read_bytes() == b'a\0b' * 100000
    assert not peer['exec']
    page.locator('#sfClose').click()
    page.locator('.xterm-helper-textarea').focus()
    page.keyboard.type('after-file-manager')
    playwright.expect(page.locator('.xterm-rows')).to_contain_text('after-file-manager')
    page.locator('#sshDisconnect').click()


def test_mobile_dirty_guard_and_conflict(page, http_server, manager, peer, tmp_path):
    root = prepare(page, http_server, manager, peer, tmp_path)
    page.set_viewport_size({'width': 390, 'height': 844})
    row(page, 'app.conf').click()
    playwright.expect(page.locator('#sfEditor')).to_have_value('port=8080\n')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('#sshFilesDialog').bounding_box()['width'] <= 390
    page.screenshot(path=str(tmp_path / 'files-mobile.png'), full_page=True)
    page.locator('#sfEditor').fill('port=9000\n')
    page.on('dialog', lambda dialog: dialog.dismiss())
    page.locator('#sfClose').click()
    playwright.expect(page.locator('#sshFilesDialog')).to_be_visible()
    (root / 'app.conf').write_text('external-update\n')
    page.locator('#sfSave').click()
    playwright.expect(page.locator('#sfFeedback')).to_contain_text('变化')
    assert (root / 'app.conf').read_text() == 'external-update\n'
    assert not list(root.glob('*.mini-deploy-backup.*'))


def test_file_api_auth_origin_owner_closed_and_large_body(http_server, manager, peer, tmp_path, monkeypatch):
    status, probe = request(http_server, 'POST', '/ssh/probe', peer['spec'])
    assert status == 200
    status, connected = request(http_server, 'POST', '/ssh/connect', {'ticket': probe['ticket'], 'accept_host_key': True,
        'auth': 'password', 'password': 'safe-password'})
    assert status == 200
    root = tmp_path / 'remote'
    enable_sftp(peer, root)
    data = {'id': connected['id'], 'action': 'list', 'path': '.'}
    assert request(http_server, 'POST', '/ssh/files', data)[0] == 200
    for field in ('origin', 'csrf', 'authenticated'):
        assert request(http_server, 'POST', '/ssh/files', data, **{field: False})[0] in {401, 403}
    assert request(http_server, 'POST', '/ssh/files', {**data, 'action': []})[0] == 400
    assert request(http_server, 'POST', '/ssh/files', {**data, 'action': 'delete'})[0] == 400
    assert request(http_server, 'POST', '/ssh/files', {**data, 'action': 'mkdir', 'path': '/proc/new', 'confirm': True})[0] == 400
    assert request(http_server, 'POST', '/ssh/files', {**data, 'id': 'another-session'})[0] in {403, 404}
    session = manager.sessions[connected['id']]
    owner = session.owner
    session.owner = 'another-owner'
    assert request(http_server, 'POST', '/ssh/files', data)[0] in {403, 404}
    # The public HTTP transport gate applies to file reads too.
    session.owner = owner
    monkeypatch.setattr(agent.Handler, '_terminal_access_allowed', lambda _: False)
    assert request(http_server, 'POST', '/ssh/files', data)[0] == 403
    monkeypatch.setattr(agent.Handler, '_terminal_access_allowed', lambda _: True)
    session.expires = time.time() - 1
    assert request(http_server, 'POST', '/ssh/files', data)[0] in {404, 410}
