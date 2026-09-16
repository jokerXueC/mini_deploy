"""Execute the installer's actual permission commands using the test user's UID."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("variable", ['$item', '$APP_HOME/$name'])
@pytest.mark.parametrize("directory", [False, True])
def test_release_permissions_use_supported_coreutils_options(tmp_path, variable, directory):
    bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash and coreutils are required")
    target = tmp_path / "release with spaces"
    if directory:
        target.mkdir()
        child = target / "-nested directory"
        child.mkdir()
        (child / "application file.py").write_text("print('fixture')\n")
        paths = [target, child, child / "application file.py"]
    else:
        target.write_text("print('fixture')\n")
        paths = [target]
    for path in paths:
        path.chmod(0o777 if path.is_dir() else 0o666)
    commands = [line.strip() for line in INSTALLER.splitlines()
                if line.strip().startswith(f'find "{variable}" -xdev') and '-exec' in line]
    assert len(commands) == 2
    # Only replace the desired owner; exercise real find/chown/chmod option parsing.
    script = 'set -euo pipefail\nitem="$TEST_TARGET"\nAPP_HOME="$TEST_PARENT"\nname="release with spaces"\n'
    script += "\n".join(command.replace('root:root', '"$(id -u):$(id -g)"') for command in commands)
    result = subprocess.run([bash, "-c", script], capture_output=True, text=True, encoding="utf-8", timeout=20,
                            env={**os.environ, "TEST_TARGET": str(target), "TEST_PARENT": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    for path in paths:
        assert path.exists()
        if os.name == "posix":
            assert path.stat().st_mode & 0o022 == 0
            assert path.stat().st_uid == os.getuid()
