from __future__ import annotations

import os
from pathlib import Path

import pytest

import agent


@pytest.fixture(autouse=True)
def isolated_maintenance_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lock_directory = tmp_path / "maintenance"
    lock_directory.mkdir(mode=0o700)
    os.chmod(lock_directory, 0o700)
    monkeypatch.setattr(agent, "MAINTENANCE_LOCK_FILE", lock_directory / "maintenance.lock")
    monkeypatch.setattr(agent, "MAINTENANCE_LOCK_OWNER_UID", getattr(os, "geteuid", lambda: 0)())
