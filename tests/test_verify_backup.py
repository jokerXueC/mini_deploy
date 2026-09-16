from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_backup as verifier


APP_HOME = "/opt/mini-deploy-agent"
ENV_FILE = "/etc/mini-deploy-agent.env"
DATA_HOME = "/var/lib/mini-deploy-agent"
LOG_HOME = "/var/log/mini-deploy-agent"
SERVICE_FILE = "/etc/systemd/system/mini-deploy-agent.service"
NGINX_CONF_FILE = "/etc/nginx/conf.d/mini-deploy-agent.conf"
AGENT_CONTENT = b"#!/usr/bin/env python3\nprint('backup fixture')\n"
DEFAULT_INCLUDED = (
    APP_HOME.lstrip("/"),
    ENV_FILE.lstrip("/"),
    DATA_HOME.lstrip("/"),
    SERVICE_FILE.lstrip("/"),
    NGINX_CONF_FILE.lstrip("/"),
)
_DEFAULT_MARKER_FINGERPRINT = object()


@dataclass(frozen=True)
class ArchiveEntry:
    name: str
    kind: str
    content: bytes = b""
    linkname: str = ""
    mode: int | None = None
    uid: int = 0


def _archive_entry_info(entry: ArchiveEntry) -> tuple[tarfile.TarInfo, io.BytesIO | None]:
    info = tarfile.TarInfo(entry.name)
    info.uid = entry.uid
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 1_700_000_000
    if entry.kind == "file":
        info.type = tarfile.REGTYPE
        info.mode = entry.mode if entry.mode is not None else 0o600
        info.size = len(entry.content)
        return info, io.BytesIO(entry.content)
    if entry.kind == "directory":
        info.type = tarfile.DIRTYPE
        info.mode = 0o700
    elif entry.kind == "symlink":
        info.type = tarfile.SYMTYPE
        info.mode = 0o777
        info.linkname = entry.linkname
    elif entry.kind == "hardlink":
        info.type = tarfile.LNKTYPE
        info.mode = 0o600
        info.linkname = entry.linkname
    elif entry.kind == "character-device":
        info.type = tarfile.CHRTYPE
    elif entry.kind == "block-device":
        info.type = tarfile.BLKTYPE
    elif entry.kind == "fifo":
        info.type = tarfile.FIFOTYPE
    elif entry.kind == "socket":
        info.type = b"s"
    else:
        raise AssertionError(f"unknown fixture archive entry kind: {entry.kind}")
    info.size = 0
    return info, None


def _write_archive(path: Path, entries: list[ArchiveEntry]) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for entry in entries:
            info, content = _archive_entry_info(entry)
            archive.addfile(info, content)
    os.chmod(path, 0o600)


def _manifest_bytes(fields: dict[str, str]) -> bytes:
    return "".join(f"{key}={value}\n" for key, value in fields.items()).encode()


def _build_bundle(
    parent: Path,
    *,
    legacy: bool = False,
    included: tuple[str, ...] = DEFAULT_INCLUDED,
    extra_entries: tuple[ArchiveEntry, ...] = (),
    omit_entries: frozenset[str] = frozenset(),
    manifest_overrides: dict[str, str] | None = None,
    marker_app_home: str = APP_HOME,
    marker_fingerprint: str | None | object = _DEFAULT_MARKER_FINGERPRINT,
) -> Path:
    bundle = parent / "mini-deploy-20260102T030405Z-test"
    bundle.mkdir(mode=0o700)
    os.chmod(bundle, 0o700)

    agent_digest = hashlib.sha256(AGENT_CONTENT).hexdigest()
    release_fingerprint = f"sha256:{agent_digest}"
    if manifest_overrides and "release_fingerprint" in manifest_overrides:
        release_fingerprint = manifest_overrides["release_fingerprint"]

    marker_lines = [
        "format=2",
        "installed_at=2026-01-02T03:04:05Z",
        f"app_home={marker_app_home}",
    ]
    effective_marker_fingerprint = marker_fingerprint
    if marker_fingerprint is _DEFAULT_MARKER_FINGERPRINT:
        effective_marker_fingerprint = release_fingerprint
    if effective_marker_fingerprint is not None:
        marker_lines.append(f"release_fingerprint={effective_marker_fingerprint}")
    marker_content = ("\n".join(marker_lines) + "\n").encode()

    app_root = APP_HOME.lstrip("/")
    entries = [
        ArchiveEntry(app_root, "directory"),
        ArchiveEntry(f"{app_root}/agent.py", "file", AGENT_CONTENT),
        ArchiveEntry(ENV_FILE.lstrip("/"), "file", b"DEPLOY_PORT=9000\n"),
        ArchiveEntry(DATA_HOME.lstrip("/"), "directory"),
        ArchiveEntry(
            f"{DATA_HOME.lstrip('/')}/projects.json",
            "file",
            b'{"projects":[{"key":"example","enabled":false}]}\n',
        ),
        ArchiveEntry(f"{DATA_HOME.lstrip('/')}/state.json", "file", b"{}\n"),
        ArchiveEntry(SERVICE_FILE.lstrip("/"), "file", b"[Service]\n"),
        ArchiveEntry(NGINX_CONF_FILE.lstrip("/"), "file", b"server {}\n"),
    ]
    if not legacy:
        entries.append(ArchiveEntry(f"{app_root}/.mini-deploy-install", "file", marker_content))
    entries = [entry for entry in entries if entry.name not in omit_entries]
    entries.extend(extra_entries)

    archive_path = bundle / "installation.tar.gz"
    _write_archive(archive_path, entries)
    archive_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()

    fields = {
        "format": "2",
        "created_at": "20260102T030405Z",
        "archive": "installation.tar.gz",
        "sha256": archive_digest,
        "release_fingerprint": release_fingerprint,
        "app_home": APP_HOME,
        "env_file": ENV_FILE,
        "data_home": DATA_HOME,
        "log_home": LOG_HOME,
        "service_name": "mini-deploy-agent",
        "service_file": SERVICE_FILE,
        "nginx_conf_file": NGINX_CONF_FILE,
        "legacy_installation": "yes" if legacy else "no",
        "included": " ".join(included),
        "managed_log_home": LOG_HOME,
        "managed_log_home_included": "no",
    }
    if manifest_overrides:
        fields.update(manifest_overrides)
    (bundle / "manifest").write_bytes(_manifest_bytes(fields))
    (bundle / "complete").write_bytes(b"complete\n")
    os.chmod(bundle / "manifest", 0o600)
    os.chmod(bundle / "complete", 0o600)
    return bundle


def _replace_manifest_lines(bundle: Path, lines: list[str]) -> None:
    (bundle / "manifest").write_bytes(("\n".join(lines) + "\n").encode())
    os.chmod(bundle / "manifest", 0o600)


def test_valid_bundle_directory_and_archive_path_are_accepted(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)

    from_directory = verifier.verify_backup(bundle, require_root_owner=False)
    from_archive = verifier.verify_backup(bundle / "installation.tar.gz", require_root_owner=False)

    assert from_directory == from_archive
    assert from_directory.member_count == 9
    assert from_directory.symlink_count == 0
    assert from_directory.hardlink_count == 0
    assert from_directory.included_roots == DEFAULT_INCLUDED
    assert not from_directory.legacy_installation


def test_archive_symlink_is_reported_without_being_followed(tmp_path: Path) -> None:
    app_root = APP_HOME.lstrip("/")
    bundle = _build_bundle(
        tmp_path,
        extra_entries=(ArchiveEntry(f"{app_root}/current", "symlink", linkname="../../outside"),),
    )

    result = verifier.verify_backup(bundle, require_root_owner=False)

    assert result.symlink_count == 1
    assert result.member_count == 10


def test_safe_hardlink_is_reported(tmp_path: Path) -> None:
    app_root = APP_HOME.lstrip("/")
    bundle = _build_bundle(
        tmp_path,
        extra_entries=(
            ArchiveEntry(
                f"{app_root}/agent-copy.py",
                "hardlink",
                linkname=f"{app_root}/agent.py",
            ),
        ),
    )

    result = verifier.verify_backup(bundle, require_root_owner=False)

    assert result.hardlink_count == 1


def test_legacy_bundle_does_not_require_install_marker(tmp_path: Path) -> None:
    data_projects = f"{DATA_HOME.lstrip('/')}/projects.json"
    app_projects = f"{APP_HOME.lstrip('/')}/projects.json"
    bundle = _build_bundle(
        tmp_path,
        legacy=True,
        omit_entries=frozenset({data_projects}),
        extra_entries=(
            ArchiveEntry(
                app_projects,
                "file",
                b'{"projects":[{"key":"legacy","enabled":false}]}\n',
            ),
        ),
    )

    result = verifier.verify_backup(bundle, require_root_owner=False)

    assert result.legacy_installation


def test_bundle_must_include_projects_config_from_app_or_data_home(tmp_path: Path) -> None:
    projects_name = f"{DATA_HOME.lstrip('/')}/projects.json"
    bundle = _build_bundle(tmp_path, omit_entries=frozenset({projects_name}))

    with pytest.raises(verifier.VerificationError, match="missing projects.json"):
        verifier.verify_backup(bundle, require_root_owner=False)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (ArchiveEntry("", "directory"), "not a regular file"),
        (ArchiveEntry("", "symlink", linkname="state.json"), "not a regular file"),
        (
            ArchiveEntry("", "hardlink", linkname=f"{DATA_HOME.lstrip('/')}/state.json"),
            "not a regular file",
        ),
        (ArchiveEntry("", "file", b"{}\n", mode=0o640), "not root-owned and private"),
        (ArchiveEntry("", "file", b"{}\n", uid=1000), "not root-owned and private"),
    ],
)
def test_archived_projects_config_must_be_a_root_private_regular_file(
    tmp_path: Path,
    entry: ArchiveEntry,
    message: str,
) -> None:
    projects_name = f"{DATA_HOME.lstrip('/')}/projects.json"
    replacement = ArchiveEntry(
        projects_name,
        entry.kind,
        entry.content,
        entry.linkname,
        entry.mode,
        entry.uid,
    )
    bundle = _build_bundle(
        tmp_path,
        omit_entries=frozenset({projects_name}),
        extra_entries=(replacement,),
    )

    with pytest.raises(verifier.VerificationError, match=message):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_unsafe_legacy_projects_config_cannot_hide_behind_safe_data_copy(tmp_path: Path) -> None:
    legacy_projects = f"{APP_HOME.lstrip('/')}/projects.json"
    bundle = _build_bundle(
        tmp_path,
        extra_entries=(ArchiveEntry(legacy_projects, "file", b"{}\n", mode=0o644),),
    )

    with pytest.raises(verifier.VerificationError, match="not root-owned and private"):
        verifier.verify_backup(bundle, require_root_owner=False)


@pytest.mark.parametrize("content", [b"", b"complete", b"complete\nextra\n"])
def test_completion_marker_must_be_exact(tmp_path: Path, content: bytes) -> None:
    bundle = _build_bundle(tmp_path)
    (bundle / "complete").write_bytes(content)

    with pytest.raises(verifier.VerificationError, match="completion marker"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_missing_completion_marker_is_rejected(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    (bundle / "complete").unlink()

    with pytest.raises(verifier.VerificationError, match="completion marker path does not exist"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_duplicate_manifest_field_is_rejected(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    manifest = (bundle / "manifest").read_text().splitlines()
    manifest.append("format=2")
    _replace_manifest_lines(bundle, manifest)

    with pytest.raises(verifier.VerificationError, match="duplicate field: format"):
        verifier.verify_backup(bundle, require_root_owner=False)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda lines: [line for line in lines if not line.startswith("service_name=")], "missing fields"),
        (lambda lines: [*lines, "surprise=value"], "unknown fields"),
    ],
)
def test_manifest_field_set_must_be_complete_and_exact(tmp_path: Path, mutate, message: str) -> None:
    bundle = _build_bundle(tmp_path)
    lines = (bundle / "manifest").read_text().splitlines()
    _replace_manifest_lines(bundle, mutate(lines))

    with pytest.raises(verifier.VerificationError, match=message):
        verifier.verify_backup(bundle, require_root_owner=False)


@pytest.mark.parametrize("archive", ["/tmp/installation.tar.gz", "../installation.tar.gz", "other.tar.gz"])
def test_manifest_archive_must_be_exact_safe_relative_name(tmp_path: Path, archive: str) -> None:
    bundle = _build_bundle(tmp_path, manifest_overrides={"archive": archive})

    with pytest.raises(verifier.VerificationError, match="manifest archive"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_archive_sha256_must_match_manifest(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, manifest_overrides={"sha256": "0" * 64})

    with pytest.raises(verifier.VerificationError, match="archive SHA256"):
        verifier.verify_backup(bundle, require_root_owner=False)


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_home": "/"},
        {"data_home": f"{APP_HOME}/data"},
        {"env_file": APP_HOME},
        {"log_home": DATA_HOME, "managed_log_home": DATA_HOME},
    ],
)
def test_managed_paths_must_be_safe_and_non_overlapping(tmp_path: Path, overrides: dict[str, str]) -> None:
    bundle = _build_bundle(tmp_path, manifest_overrides=overrides)

    with pytest.raises(verifier.VerificationError, match="supported absolute path|dangerous|overlap"):
        verifier.verify_backup(bundle, require_root_owner=False)


@pytest.mark.parametrize("name", ["/etc/passwd", "../escape", "opt/mini-deploy-agent/../escape"])
def test_tar_member_names_cannot_be_absolute_or_escape(tmp_path: Path, name: str) -> None:
    bundle = _build_bundle(tmp_path, extra_entries=(ArchiveEntry(name, "file", b"unsafe"),))

    with pytest.raises(verifier.VerificationError, match="tar member name"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_every_tar_member_must_be_within_an_included_root(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, extra_entries=(ArchiveEntry("etc/passwd", "file", b"unsafe"),))

    with pytest.raises(verifier.VerificationError, match="outside manifest included roots"):
        verifier.verify_backup(bundle, require_root_owner=False)


@pytest.mark.parametrize("kind", ["character-device", "block-device", "fifo", "socket"])
def test_tar_special_members_are_rejected(tmp_path: Path, kind: str) -> None:
    app_root = APP_HOME.lstrip("/")
    bundle = _build_bundle(
        tmp_path,
        extra_entries=(ArchiveEntry(f"{app_root}/special", kind),),
    )

    with pytest.raises(verifier.VerificationError, match="forbidden special type"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_every_declared_included_root_must_exist_in_tar(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, omit_entries=frozenset({SERVICE_FILE.lstrip("/")}))

    with pytest.raises(verifier.VerificationError, match="missing included root"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_tar_member_cannot_descend_through_symlink(tmp_path: Path) -> None:
    app_root = APP_HOME.lstrip("/")
    bundle = _build_bundle(
        tmp_path,
        extra_entries=(
            ArchiveEntry(f"{app_root}/release", "symlink", linkname="release-v1"),
            ArchiveEntry(f"{app_root}/release/settings.json", "file", b"{}\n"),
        ),
    )

    with pytest.raises(verifier.VerificationError, match="traverses a non-directory member"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_nonlegacy_marker_is_bound_to_manifest_app_home(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, marker_app_home="/opt/another-app")

    with pytest.raises(verifier.VerificationError, match="different app_home"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_agent_fingerprint_must_match_manifest(tmp_path: Path) -> None:
    wrong_fingerprint = f"sha256:{'0' * 64}"
    bundle = _build_bundle(tmp_path, manifest_overrides={"release_fingerprint": wrong_fingerprint})

    with pytest.raises(verifier.VerificationError, match="agent.py fingerprint"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_locally_patched_agent_can_have_an_older_valid_marker_fingerprint(tmp_path: Path) -> None:
    old_fingerprint = f"sha256:{'1' * 64}"
    bundle = _build_bundle(tmp_path, marker_fingerprint=old_fingerprint)

    result = verifier.verify_backup(bundle, require_root_owner=False)

    assert result.release_fingerprint == hashlib.sha256(AGENT_CONTENT).hexdigest()


def test_marker_fingerprint_must_have_valid_syntax_when_present(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, marker_fingerprint="sha256:not-a-digest")

    with pytest.raises(verifier.VerificationError, match="marker release fingerprint is invalid"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_duplicate_tar_member_is_rejected(tmp_path: Path) -> None:
    app_root = APP_HOME.lstrip("/")
    bundle = _build_bundle(
        tmp_path,
        extra_entries=(ArchiveEntry(f"{app_root}/agent.py", "file", AGENT_CONTENT),),
    )

    with pytest.raises(verifier.VerificationError, match="duplicate member"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_tar_member_count_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _build_bundle(tmp_path)
    monkeypatch.setattr(verifier, "MAX_TAR_MEMBERS", 2)

    with pytest.raises(verifier.VerificationError, match="member safety limit"):
        verifier.verify_backup(bundle, require_root_owner=False)


def test_tar_declared_regular_file_total_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _build_bundle(tmp_path)
    monkeypatch.setattr(verifier, "MAX_DECLARED_REGULAR_FILE_BYTES", 1)

    with pytest.raises(verifier.VerificationError, match="declared regular-file safety limit"):
        verifier.verify_backup(bundle, require_root_owner=False)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are a Linux deployment invariant")
def test_bundle_files_must_be_private(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    os.chmod(bundle / "manifest", 0o640)

    with pytest.raises(verifier.VerificationError, match="manifest must be private"):
        verifier.verify_backup(bundle, require_root_owner=False)


@pytest.mark.skipif(os.name != "posix", reason="symlink creation is not portable on Windows")
def test_bundle_path_and_artifacts_must_not_be_symlinks(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    target = tmp_path / "manifest-target"
    target.write_bytes((bundle / "manifest").read_bytes())
    os.chmod(target, 0o600)
    (bundle / "manifest").unlink()
    (bundle / "manifest").symlink_to(target)

    with pytest.raises(verifier.VerificationError, match="symbolic link"):
        verifier.verify_backup(bundle, require_root_owner=False)

    alias = tmp_path / "bundle-alias"
    alias.symlink_to(bundle, target_is_directory=True)
    with pytest.raises(verifier.VerificationError, match="symbolic link"):
        verifier.verify_backup(alias, require_root_owner=False)


def test_bundle_artifacts_must_not_have_hard_link_aliases(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    os.link(bundle / "manifest", tmp_path / "manifest-alias")

    with pytest.raises(verifier.VerificationError, match="exactly one hard link"):
        verifier.verify_backup(bundle, require_root_owner=False)


@pytest.mark.skipif(os.name != "posix", reason="trusted-owner and mode checks target Linux servers")
def test_root_mode_rejects_group_or_other_writable_parent_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    untrusted_parent = tmp_path / "untrusted"
    untrusted_parent.mkdir(mode=0o700)
    bundle = _build_bundle(untrusted_parent)
    real_lstat = os.lstat

    def fake_root_owned_lstat(path):
        metadata = real_lstat(path)
        mode = metadata.st_mode
        if Path(path) == untrusted_parent:
            mode |= stat.S_IWOTH
        return SimpleNamespace(st_mode=mode, st_uid=0)

    monkeypatch.setattr(verifier.os, "lstat", fake_root_owned_lstat)

    with pytest.raises(verifier.VerificationError, match="group/other writable"):
        verifier._validate_trusted_directory_chain(bundle, require_root_owner=True)
