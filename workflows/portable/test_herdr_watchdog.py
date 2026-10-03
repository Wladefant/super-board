"""Runs the isolated herdr watchdog proof (test_herdr_watchdog.ps1) when herdr is installed.

The PowerShell script starts and kills servers of a throwaway named session only; it refuses the
default session, so it never touches the shared herdr server.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("test_herdr_watchdog.ps1")


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("herdr") is None or shutil.which("powershell") is None,
    reason="needs Windows PowerShell and herdr on PATH",
)
def test_watchdog_restarts_killed_server_without_client_and_honours_stop_and_cap() -> None:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RESULT failures=0" in proc.stdout
