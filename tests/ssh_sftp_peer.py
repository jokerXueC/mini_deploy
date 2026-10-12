"""Local SFTP test adapter, confined to a pytest temporary directory."""
import os
from pathlib import Path
import posixpath

import paramiko


class LocalFiles(paramiko.SFTPServerInterface):
    def __init__(self, server, *, root):
        super().__init__(server)
        self.root = Path(root).resolve()

    def path(self, path):
        target = self.root.joinpath(*posixpath.normpath('/' + path).strip('/').split('/'))
        if not target.resolve().is_relative_to(self.root):
            raise PermissionError('outside test root')
        return target

    def canonicalize(self, path):
        try:
            return '/' + self.path(path).resolve().relative_to(self.root).as_posix().removeprefix('.')
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.path(path).lstat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def list_folder(self, path):
        try:
            items = []
            for item in self.path(path).iterdir():
                info = paramiko.SFTPAttributes.from_stat(item.lstat())
                info.filename = item.name
                items.append(info)
            return items
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attr):
        try:
            descriptor = os.open(self.path(path), flags, 0o600)
            mode = 'r+b' if flags & os.O_RDWR else 'wb' if flags & os.O_WRONLY else 'rb'
            stream = os.fdopen(descriptor, mode)
            handle = paramiko.SFTPHandle(flags)
            if not flags & os.O_WRONLY:
                handle.readfile = stream
            if flags & (os.O_WRONLY | os.O_RDWR):
                handle.writefile = stream
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def chattr(self, path, attr):
        try:
            target = self.path(path)
            if attr.st_mode is not None:
                target.chmod(attr.st_mode)
            if attr.st_uid is not None and os.name != 'nt':
                os.chown(target, attr.st_uid, attr.st_gid)
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def posix_rename(self, old, new):
        try:
            os.replace(self.path(old), self.path(new))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


def enable_sftp(peer, root):
    root.mkdir(exist_ok=True)
    for transport in peer['transports']:
        transport.set_subsystem_handler('sftp', paramiko.SFTPServer, sftp_si=LocalFiles, root=root)
