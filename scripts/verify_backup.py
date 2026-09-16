#!/usr/bin/env python3
"""Read-only validator for mini_deploy format=2 backup bundles."""

from __future__ import annotations

import argparse
import hashlib
import os
import posixpath
import re
import stat
import sys
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator, Sequence


MANIFEST_FIELDS = frozenset({
    "format",
    "created_at",
    "archive",
    "sha256",
    "release_fingerprint",
    "app_home",
    "env_file",
    "data_home",
    "log_home",
    "service_name",
    "service_file",
    "nginx_conf_file",
    "legacy_installation",
    "included",
    "managed_log_home",
    "managed_log_home_included",
})
MANAGED_DIRECTORY_FIELDS = ("app_home", "data_home", "log_home")
CONTROL_FILE_FIELDS = ("env_file", "service_file", "nginx_conf_file")
ARCHIVABLE_PATH_FIELDS = ("app_home", "env_file", "data_home", "service_file", "nginx_conf_file")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ABSOLUTE_PATH_RE = re.compile(r"/[A-Za-z0-9_./@+\-]+")
SERVICE_NAME_RE = re.compile(r"[A-Za-z0-9_.@\-]+")
MAX_MANIFEST_BYTES = 64 * 1024
MAX_MARKER_BYTES = 64 * 1024
MAX_TAR_MEMBERS = 100_000
MAX_DECLARED_REGULAR_FILE_BYTES = 128 * 1024**3
CHUNK_SIZE = 1024 * 1024

DANGEROUS_MANAGED_DIRECTORIES = frozenset({
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/home",
    "/lib",
    "/lib64",
    "/media",
    "/mnt",
    "/opt",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/srv",
    "/sys",
    "/tmp",
    "/usr",
    "/usr/local",
    "/usr/local/bin",
    "/usr/local/lib",
    "/usr/local/sbin",
    "/usr/local/share",
    "/var",
    "/var/backups",
    "/var/lib",
    "/var/log",
    "/var/tmp",
    "/var/www",
})
DANGEROUS_MANAGED_PREFIXES = (
    "/bin/",
    "/boot/",
    "/dev/",
    "/etc/",
    "/home/",
    "/lib/",
    "/lib64/",
    "/proc/",
    "/root/",
    "/run/",
    "/sbin/",
    "/sys/",
    "/tmp/",
    "/usr/bin/",
    "/usr/lib/",
    "/usr/lib64/",
    "/usr/local/bin/",
    "/usr/local/lib/",
    "/usr/local/sbin/",
    "/usr/local/share/",
    "/usr/sbin/",
    "/var/tmp/",
)


class VerificationError(ValueError):
    """Raised when a bundle cannot be proven safe and complete."""


@dataclass(frozen=True)
class VerificationResult:
    bundle: Path
    archive: Path
    archive_sha256: str
    release_fingerprint: str
    member_count: int
    symlink_count: int
    hardlink_count: int
    included_roots: tuple[str, ...]
    legacy_installation: bool


def _absolute_path(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_no_symlink_components(path: Path, label: str) -> None:
    current = path
    while True:
        try:
            metadata = os.lstat(current)
        except FileNotFoundError as exc:
            raise VerificationError(f"{label} path does not exist: {current}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise VerificationError(f"{label} path must not traverse a symbolic link: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _validate_path_metadata(
    path: Path,
    label: str,
    *,
    directory: bool,
    require_root_owner: bool,
) -> os.stat_result:
    _assert_no_symlink_components(path, label)
    metadata = os.lstat(path)
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected:
        expected_type = "directory" if directory else "regular file"
        raise VerificationError(f"{label} must be a {expected_type}: {path}")
    if not directory and metadata.st_nlink != 1:
        raise VerificationError(f"{label} must have exactly one hard link: {path}")
    if require_root_owner and getattr(metadata, "st_uid", None) != 0:
        raise VerificationError(f"{label} must be root-owned: {path}")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise VerificationError(f"{label} must be private (no group/other permissions): {path}")
    return metadata


def _validate_trusted_directory_chain(path: Path, *, require_root_owner: bool) -> None:
    if not require_root_owner:
        return

    current = path
    while True:
        try:
            metadata = os.lstat(current)
        except FileNotFoundError as exc:
            raise VerificationError(f"bundle parent path does not exist: {current}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise VerificationError(f"bundle path must not traverse a symbolic link: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise VerificationError(f"bundle parent path must be a directory: {current}")
        if getattr(metadata, "st_uid", None) != 0:
            raise VerificationError(f"bundle parent path must be root-owned: {current}")
        if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o022:
            raise VerificationError(f"bundle parent path must not be group/other writable: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


@contextmanager
def _open_private_file(
    path: Path,
    label: str,
    *,
    require_root_owner: bool,
) -> Iterator[BinaryIO]:
    metadata = _validate_path_metadata(
        path,
        label,
        directory=False,
        require_root_owner=require_root_owner,
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise VerificationError(f"{label} changed to a non-regular file while opening: {path}")
        if opened_metadata.st_nlink != 1:
            raise VerificationError(f"{label} must have exactly one hard link: {path}")
        if (metadata.st_dev, metadata.st_ino) != (opened_metadata.st_dev, opened_metadata.st_ino):
            raise VerificationError(f"{label} changed while opening: {path}")
        with os.fdopen(descriptor, "rb") as file_handle:
            descriptor = -1
            yield file_handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_small_private_file(
    path: Path,
    label: str,
    *,
    limit: int,
    require_root_owner: bool,
) -> bytes:
    with _open_private_file(path, label, require_root_owner=require_root_owner) as file_handle:
        content = file_handle.read(limit + 1)
    if len(content) > limit:
        raise VerificationError(f"{label} exceeds the {limit}-byte safety limit")
    return content


def _parse_key_value_lines(content: bytes, label: str) -> dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError(f"{label} is not valid UTF-8") from exc
    if "\x00" in text or "\r" in text:
        raise VerificationError(f"{label} contains unsupported control characters")
    if not text.endswith("\n"):
        raise VerificationError(f"{label} must end with a newline")

    fields: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line or "=" not in line:
            raise VerificationError(f"{label} line {line_number} is not key=value")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise VerificationError(f"{label} line {line_number} has an invalid field name")
        if key in fields:
            raise VerificationError(f"{label} contains duplicate field: {key}")
        if not value or value != value.strip():
            raise VerificationError(f"{label} field {key} is empty or has surrounding whitespace")
        fields[key] = value
    return fields


def _parse_manifest(content: bytes) -> dict[str, str]:
    fields = _parse_key_value_lines(content, "manifest")
    missing = MANIFEST_FIELDS - fields.keys()
    unknown = fields.keys() - MANIFEST_FIELDS
    if missing:
        raise VerificationError(f"manifest is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise VerificationError(f"manifest contains unknown fields: {', '.join(sorted(unknown))}")
    if fields["format"] != "2":
        raise VerificationError("manifest format must be 2")
    try:
        parsed_timestamp = datetime.strptime(fields["created_at"], "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise VerificationError("manifest created_at is invalid") from exc
    if parsed_timestamp.strftime("%Y%m%dT%H%M%SZ") != fields["created_at"]:
        raise VerificationError("manifest created_at is not canonical")
    if not SHA256_RE.fullmatch(fields["sha256"]):
        raise VerificationError("manifest sha256 is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", fields["release_fingerprint"]):
        raise VerificationError("manifest release_fingerprint is invalid")
    if fields["legacy_installation"] not in {"yes", "no"}:
        raise VerificationError("manifest legacy_installation must be yes or no")
    if fields["managed_log_home_included"] != "no":
        raise VerificationError("manifest managed_log_home_included must be no")
    return fields


def _validate_absolute_managed_path(value: str, field: str) -> str:
    if not ABSOLUTE_PATH_RE.fullmatch(value):
        raise VerificationError(f"manifest {field} is not a supported absolute path")
    if value != posixpath.normpath(value) or "//" in value:
        raise VerificationError(f"manifest {field} is not canonical")
    return value


def _paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def _validate_managed_paths(fields: dict[str, str]) -> None:
    for field in (*MANAGED_DIRECTORY_FIELDS, *CONTROL_FILE_FIELDS, "managed_log_home"):
        _validate_absolute_managed_path(fields[field], field)

    if fields["managed_log_home"] != fields["log_home"]:
        raise VerificationError("managed_log_home must equal log_home")

    directories = [fields[field] for field in MANAGED_DIRECTORY_FIELDS]
    for field, directory in zip(MANAGED_DIRECTORY_FIELDS, directories):
        if directory in DANGEROUS_MANAGED_DIRECTORIES or directory.startswith(DANGEROUS_MANAGED_PREFIXES):
            raise VerificationError(f"manifest {field} is a dangerous managed directory: {directory}")
    for index, left in enumerate(directories):
        for right in directories[index + 1 :]:
            if _paths_overlap(left, right):
                raise VerificationError(f"managed directories overlap: {left} and {right}")

    controls = [fields[field] for field in CONTROL_FILE_FIELDS]
    for control in controls:
        for directory in directories:
            if _paths_overlap(control, directory):
                raise VerificationError(f"control file overlaps a managed directory: {control} and {directory}")
    for index, left in enumerate(controls):
        for right in controls[index + 1 :]:
            if _paths_overlap(left, right):
                raise VerificationError(f"control files overlap: {left} and {right}")

    env_name = PurePosixPath(fields["env_file"]).name
    if not (env_name.endswith(".env") or "mini-deploy" in env_name or "mini_deploy" in env_name):
        raise VerificationError("manifest env_file is not a dedicated mini_deploy environment file")
    if not PurePosixPath(fields["service_file"]).name.endswith(".service"):
        raise VerificationError("manifest service_file must end in .service")
    if not PurePosixPath(fields["nginx_conf_file"]).name.endswith(".conf"):
        raise VerificationError("manifest nginx_conf_file must end in .conf")
    if not SERVICE_NAME_RE.fullmatch(fields["service_name"]):
        raise VerificationError("manifest service_name is invalid")


def _normalize_relative_path(value: str, label: str, *, allow_trailing_slash: bool = False) -> str:
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        raise VerificationError(f"{label} must be a safe relative POSIX path")
    candidate = value
    if allow_trailing_slash and candidate.endswith("/"):
        candidate = candidate.rstrip("/")
    parts = candidate.split("/")
    if not candidate or any(part in {"", ".", ".."} for part in parts):
        raise VerificationError(f"{label} contains an empty, dot, or parent component")
    normalized = "/".join(parts)
    if normalized != posixpath.normpath(normalized):
        raise VerificationError(f"{label} is not canonical")
    return normalized


def _validate_archive_field(value: str) -> str:
    normalized = _normalize_relative_path(value, "manifest archive")
    if normalized != "installation.tar.gz":
        raise VerificationError("manifest archive must be the relative path installation.tar.gz")
    return normalized


def _validate_included_roots(fields: dict[str, str]) -> tuple[str, ...]:
    included_text = fields["included"]
    if "  " in included_text:
        raise VerificationError("manifest included must use single-space separators")
    roots = tuple(included_text.split(" "))
    if not roots or any(not root for root in roots):
        raise VerificationError("manifest included must contain at least one root")
    if len(set(roots)) != len(roots):
        raise VerificationError("manifest included contains duplicate roots")

    expected = {
        _validate_absolute_managed_path(fields[field], field).lstrip("/")
        for field in ARCHIVABLE_PATH_FIELDS
    }
    normalized_roots = tuple(
        _normalize_relative_path(root, "manifest included root")
        for root in roots
    )
    unknown = set(normalized_roots) - expected
    if unknown:
        raise VerificationError(f"manifest included contains unmanaged roots: {', '.join(sorted(unknown))}")
    app_root = fields["app_home"].lstrip("/")
    if app_root not in normalized_roots:
        raise VerificationError("manifest included must contain app_home")
    log_root = fields["log_home"].lstrip("/")
    if any(_paths_overlap(root, log_root) for root in normalized_roots):
        raise VerificationError("manifest included must not contain log_home")
    for index, left in enumerate(normalized_roots):
        for right in normalized_roots[index + 1 :]:
            if _paths_overlap(left, right):
                raise VerificationError(f"manifest included roots overlap: {left} and {right}")
    return normalized_roots


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "directory"
    if member.isreg():
        return "file"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    raise VerificationError(f"tar member has a forbidden special type: {member.name}")


def _member_is_included(name: str, roots: tuple[str, ...]) -> bool:
    return any(name == root or name.startswith(f"{root}/") for root in roots)


def _hash_stream(file_handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = file_handle.read(CHUNK_SIZE)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _hash_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    file_handle = archive.extractfile(member)
    if file_handle is None:
        raise VerificationError(f"tar member cannot be read as a regular file: {member.name}")
    with file_handle:
        return _hash_stream(file_handle)


def _read_tar_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    limit: int,
    label: str,
) -> bytes:
    if member.size > limit:
        raise VerificationError(f"{label} exceeds the {limit}-byte safety limit")
    file_handle = archive.extractfile(member)
    if file_handle is None:
        raise VerificationError(f"{label} cannot be read")
    with file_handle:
        content = file_handle.read(limit + 1)
    if len(content) > limit:
        raise VerificationError(f"{label} exceeds the {limit}-byte safety limit")
    return content


def _resolve_hardlink(
    name: str,
    members: dict[str, tuple[tarfile.TarInfo, str]],
) -> None:
    visited = {name}
    current = name
    while True:
        member, kind = members[current]
        if kind == "file":
            return
        if kind != "hardlink":
            raise VerificationError(f"hardlink does not resolve to a regular file: {name}")
        target = _normalize_relative_path(member.linkname, f"hardlink target for {name}")
        if target not in members:
            raise VerificationError(f"hardlink target is missing from archive: {name} -> {target}")
        if target in visited:
            raise VerificationError(f"hardlink cycle detected at: {name}")
        visited.add(target)
        current = target


def _verify_tar(
    archive_file: BinaryIO,
    fields: dict[str, str],
    included_roots: tuple[str, ...],
) -> tuple[int, int, int, str]:
    try:
        with tarfile.open(fileobj=archive_file, mode="r:gz") as archive:
            members: dict[str, tuple[tarfile.TarInfo, str]] = {}
            symlink_count = 0
            hardlink_count = 0
            declared_regular_file_bytes = 0
            for member_count, member in enumerate(archive, start=1):
                if member_count > MAX_TAR_MEMBERS:
                    raise VerificationError(f"tar archive exceeds the {MAX_TAR_MEMBERS}-member safety limit")
                name = _normalize_relative_path(
                    member.name,
                    "tar member name",
                    allow_trailing_slash=member.isdir(),
                )
                if name in members:
                    raise VerificationError(f"tar contains duplicate member: {name}")
                if not _member_is_included(name, included_roots):
                    raise VerificationError(f"tar member is outside manifest included roots: {name}")
                kind = _member_kind(member)
                if kind == "file":
                    if member.size < 0:
                        raise VerificationError(f"tar regular file has a negative declared size: {name}")
                    declared_regular_file_bytes += member.size
                    if declared_regular_file_bytes > MAX_DECLARED_REGULAR_FILE_BYTES:
                        raise VerificationError(
                            "tar archive exceeds the "
                            f"{MAX_DECLARED_REGULAR_FILE_BYTES}-byte declared regular-file safety limit"
                        )
                members[name] = (member, kind)
                symlink_count += int(kind == "symlink")
                hardlink_count += int(kind == "hardlink")

            if not members:
                raise VerificationError("tar archive contains no members")
            for root in included_roots:
                if root not in members:
                    raise VerificationError(f"tar archive is missing included root: {root}")

            directory_roots = {fields["app_home"].lstrip("/")}
            data_root = fields["data_home"].lstrip("/")
            if data_root in included_roots:
                directory_roots.add(data_root)
            for root in directory_roots:
                if members[root][1] != "directory":
                    raise VerificationError(f"included managed directory is not a tar directory: {root}")
            for field in CONTROL_FILE_FIELDS:
                root = fields[field].lstrip("/")
                if root in included_roots and members[root][1] != "file":
                    raise VerificationError(f"included control file is not a regular tar file: {root}")

            for name in members:
                components = name.split("/")
                for index in range(1, len(components)):
                    ancestor = "/".join(components[:index])
                    if ancestor in members and members[ancestor][1] != "directory":
                        raise VerificationError(f"tar member traverses a non-directory member: {name}")
            for name, (_, kind) in members.items():
                if kind == "hardlink":
                    _resolve_hardlink(name, members)

            app_root = fields["app_home"].lstrip("/")
            projects_candidates = (
                f"{app_root}/projects.json",
                f"{data_root}/projects.json",
            )
            archived_projects = [name for name in projects_candidates if name in members]
            if not archived_projects:
                raise VerificationError(
                    "tar archive is missing projects.json from both app_home and data_home"
                )
            for projects_name in archived_projects:
                projects_member, projects_kind = members[projects_name]
                if projects_kind != "file":
                    raise VerificationError(
                        f"archived projects.json is not a regular file: {projects_name}"
                    )
                if projects_member.uid != 0 or stat.S_IMODE(projects_member.mode) & 0o077:
                    raise VerificationError(
                        f"archived projects.json is not root-owned and private: {projects_name}"
                    )

            agent_name = f"{app_root}/agent.py"
            if agent_name not in members or members[agent_name][1] != "file":
                raise VerificationError("tar archive is missing a regular app_home/agent.py")
            agent_fingerprint = _hash_tar_member(archive, members[agent_name][0])
            expected_fingerprint = fields["release_fingerprint"].removeprefix("sha256:")
            if agent_fingerprint != expected_fingerprint:
                raise VerificationError("agent.py fingerprint does not match release_fingerprint")

            if fields["legacy_installation"] == "no":
                marker_name = f"{app_root}/.mini-deploy-install"
                if marker_name not in members or members[marker_name][1] != "file":
                    raise VerificationError("non-legacy archive is missing a regular app installation marker")
                marker_content = _read_tar_member(
                    archive,
                    members[marker_name][0],
                    limit=MAX_MARKER_BYTES,
                    label="app installation marker",
                )
                marker = _parse_key_value_lines(marker_content, "app installation marker")
                if marker.get("format") not in {"1", "2"}:
                    raise VerificationError("app installation marker format is invalid")
                if marker.get("app_home") != fields["app_home"]:
                    raise VerificationError("app installation marker is bound to a different app_home")
                marker_fingerprint = marker.get("release_fingerprint")
                if marker_fingerprint is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", marker_fingerprint):
                    raise VerificationError("app installation marker release fingerprint is invalid")

            return len(members), symlink_count, hardlink_count, agent_fingerprint
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise VerificationError(f"archive is not a readable gzip tar: {exc}") from exc


def _locate_bundle(source: Path) -> tuple[Path, Path | None]:
    _assert_no_symlink_components(source, "input")
    metadata = os.lstat(source)
    if stat.S_ISDIR(metadata.st_mode):
        return source, None
    if stat.S_ISREG(metadata.st_mode) and source.name == "installation.tar.gz":
        return source.parent, source
    raise VerificationError("input must be a backup bundle directory or installation.tar.gz")


def verify_backup(
    source: str | os.PathLike[str],
    *,
    require_root_owner: bool = True,
) -> VerificationResult:
    """Validate a bundle without extracting it or modifying the filesystem."""

    input_path = _absolute_path(source)
    bundle, requested_archive = _locate_bundle(input_path)
    _validate_trusted_directory_chain(bundle, require_root_owner=require_root_owner)
    _validate_path_metadata(
        bundle,
        "bundle",
        directory=True,
        require_root_owner=require_root_owner,
    )

    complete_content = _read_small_private_file(
        bundle / "complete",
        "completion marker",
        limit=32,
        require_root_owner=require_root_owner,
    )
    if complete_content != b"complete\n":
        raise VerificationError("completion marker must contain exactly 'complete\\n'")

    manifest_content = _read_small_private_file(
        bundle / "manifest",
        "manifest",
        limit=MAX_MANIFEST_BYTES,
        require_root_owner=require_root_owner,
    )
    fields = _parse_manifest(manifest_content)
    _validate_managed_paths(fields)
    included_roots = _validate_included_roots(fields)
    archive_relative = _validate_archive_field(fields["archive"])
    archive_path = bundle.joinpath(*PurePosixPath(archive_relative).parts)
    if requested_archive is not None and os.path.normcase(str(requested_archive)) != os.path.normcase(str(archive_path)):
        raise VerificationError("requested archive does not match manifest archive")

    with _open_private_file(
        archive_path,
        "archive",
        require_root_owner=require_root_owner,
    ) as archive_file:
        archive_sha256 = _hash_stream(archive_file)
        if archive_sha256 != fields["sha256"]:
            raise VerificationError("archive SHA256 does not match manifest")
        archive_file.seek(0)
        member_count, symlink_count, hardlink_count, release_fingerprint = _verify_tar(
            archive_file,
            fields,
            included_roots,
        )

    return VerificationResult(
        bundle=bundle,
        archive=archive_path,
        archive_sha256=archive_sha256,
        release_fingerprint=release_fingerprint,
        member_count=member_count,
        symlink_count=symlink_count,
        hardlink_count=hardlink_count,
        included_roots=included_roots,
        legacy_installation=fields["legacy_installation"] == "yes",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="backup bundle directory or its installation.tar.gz")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        result = verify_backup(arguments.source)
    except (OSError, VerificationError) as exc:
        print(f"backup verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        "backup verified "
        f"bundle={result.bundle} members={result.member_count} "
        f"symlinks={result.symlink_count} hardlinks={result.hardlink_count} "
        f"sha256={result.archive_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
