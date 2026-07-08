"""Phase 12: the maintenance shell scripts must be syntactically valid (bash -n)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPTS = ["backup.sh", "restore.sh", "list_backups.sh", "healthcheck.sh"]


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_passes_bash_n(name: str) -> None:
    path = SCRIPTS_DIR / name
    assert path.exists(), f"missing script: {name}"
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed for {name}:\n{result.stderr}"


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_sets_safe_mode(name: str) -> None:
    text = (SCRIPTS_DIR / name).read_text()
    assert "set -euo pipefail" in text, f"{name} must use 'set -euo pipefail'"


def test_restore_script_requires_confirmation_phrase() -> None:
    assert "RESTORE_DIGITALCORE" in (SCRIPTS_DIR / "restore.sh").read_text()
