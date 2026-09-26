"""Tests for host_status.py — classify, format, portability, and hermetic CLI."""
from __future__ import annotations

import io
import json
import platform
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

# Import the module under test from workflows/portable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflows" / "portable"))
import host_status


# ---------- classify ----------


class TestClassify:
    """Threshold-based state classification."""

    def test_ok_normal_load(self) -> None:
        state, reasons = host_status.classify(ram_percent=60.0, disk_free_gb=100.0)
        assert state == "ok"
        assert reasons == []

    def test_ok_just_below_reap_thresholds(self) -> None:
        state, reasons = host_status.classify(ram_percent=84.9, disk_free_gb=30.0)
        assert state == "ok"
        assert reasons == []

    def test_reap_ram_at_boundary(self) -> None:
        state, reasons = host_status.classify(ram_percent=85.0, disk_free_gb=100.0)
        assert state == "reap"
        assert "RAM >= 85%" in reasons

    def test_reap_ram_upper_boundary(self) -> None:
        state, reasons = host_status.classify(ram_percent=89.9, disk_free_gb=100.0)
        assert state == "reap"
        assert "RAM >= 85%" in reasons

    def test_reap_disk_at_boundary(self) -> None:
        state, reasons = host_status.classify(ram_percent=50.0, disk_free_gb=29.9)
        assert state == "reap"
        assert "disk < 30 GB" in reasons

    def test_reap_both_triggers(self) -> None:
        state, reasons = host_status.classify(ram_percent=87.0, disk_free_gb=25.0)
        assert state == "reap"
        assert len(reasons) == 2
        assert "RAM >= 85%" in reasons
        assert "disk < 30 GB" in reasons

    def test_wait_ram_at_boundary(self) -> None:
        state, reasons = host_status.classify(ram_percent=90.0, disk_free_gb=100.0)
        assert state == "wait"
        assert "RAM >= 90%" in reasons

    def test_wait_ram_upper_boundary(self) -> None:
        state, reasons = host_status.classify(ram_percent=94.9, disk_free_gb=100.0)
        assert state == "wait"
        assert "RAM >= 90%" in reasons

    def test_wait_ram_with_reap_disk(self) -> None:
        state, reasons = host_status.classify(ram_percent=92.0, disk_free_gb=25.0)
        assert state == "wait"
        assert "RAM >= 90%" in reasons
        assert "disk < 30 GB" in reasons

    def test_no_spawn_ram_at_boundary(self) -> None:
        state, reasons = host_status.classify(ram_percent=95.0, disk_free_gb=100.0)
        assert state == "no_spawn"
        assert "RAM >= 95%" in reasons

    def test_no_spawn_disk_at_boundary(self) -> None:
        state, reasons = host_status.classify(ram_percent=50.0, disk_free_gb=19.9)
        assert state == "no_spawn"
        assert "disk < 20 GB" in reasons

    def test_no_spawn_both_triggers(self) -> None:
        state, reasons = host_status.classify(ram_percent=96.0, disk_free_gb=15.0)
        assert state == "no_spawn"
        assert len(reasons) == 2
        assert "RAM >= 95%" in reasons
        assert "disk < 20 GB" in reasons

    def test_no_spawn_trumps_wait_and_reap(self) -> None:
        """When no_spawn, wait, and reap conditions exist, no_spawn wins."""
        state, reasons = host_status.classify(ram_percent=96.0, disk_free_gb=25.0)
        assert state == "no_spawn"
        assert "RAM >= 95%" in reasons

    def test_zero_ram(self) -> None:
        state, _ = host_status.classify(ram_percent=0.0, disk_free_gb=500.0)
        assert state == "ok"

    def test_hundred_percent_ram(self) -> None:
        state, reasons = host_status.classify(ram_percent=100.0, disk_free_gb=500.0)
        assert state == "no_spawn"
        assert "RAM >= 95%" in reasons

    def test_zero_disk(self) -> None:
        state, reasons = host_status.classify(ram_percent=50.0, disk_free_gb=0.0)
        assert state == "no_spawn"
        assert "disk < 20 GB" in reasons


# ---------- format_human ----------


class TestFormatHuman:
    def test_ok_output(self) -> None:
        ram = {"total_gb": 32.0, "used_gb": 16.0, "percent": 50.0}
        disk = {"total_gb": 500.0, "free_gb": 200.0, "percent_used": 60.0, "drive": "C:\\"}
        output = host_status.format_human(ram, disk, "ok", [])
        assert "RAM:  16.0 / 32.0 GB (50%)" in output
        assert "Disk: 200.0 / 500.0 GB free (60.0% used)  [C:\\]" in output
        assert "State: ok" in output

    def test_reap_output(self) -> None:
        ram = {"total_gb": 32.0, "used_gb": 28.0, "percent": 87.5}
        disk = {"total_gb": 500.0, "free_gb": 25.0, "percent_used": 95.0, "drive": "C:\\"}
        output = host_status.format_human(ram, disk, "reap", ["RAM >= 85%", "disk < 30 GB"])
        assert "[reap: RAM >= 85%; disk < 30 GB]" in output
        assert "State: reap" in output

    def test_wait_output(self) -> None:
        ram = {"total_gb": 32.0, "used_gb": 29.0, "percent": 90.6}
        disk = {"total_gb": 500.0, "free_gb": 200.0, "percent_used": 60.0, "drive": "C:\\"}
        output = host_status.format_human(ram, disk, "wait", ["RAM >= 90%"])
        assert "[wait: RAM >= 90%]" in output
        assert "State: wait" in output

    def test_no_spawn_output(self) -> None:
        ram = {"total_gb": 32.0, "used_gb": 31.0, "percent": 96.8}
        disk = {"total_gb": 500.0, "free_gb": 15.0, "percent_used": 97.0, "drive": "C:\\"}
        output = host_status.format_human(ram, disk, "no_spawn", ["RAM >= 95%", "disk < 20 GB"])
        assert "[no_spawn: RAM >= 95%; disk < 20 GB]" in output
        assert "State: no_spawn" in output


# ---------- format_json ----------


class TestFormatJson:
    def test_valid_json(self) -> None:
        ram = {"total_gb": 32.0, "used_gb": 16.0, "available_gb": 16.0, "percent": 50.0}
        disk = {"total_gb": 500.0, "free_gb": 200.0, "percent_used": 60.0, "drive": "C:\\"}
        raw = host_status.format_json(ram, disk, "ok", [])
        parsed = json.loads(raw)
        assert parsed["ram"] == ram
        assert parsed["disk"] == disk
        assert parsed["state"] == "ok"
        assert parsed["reasons"] == []


# ---------- portability ----------


class TestPortability:
    def test_default_drive_windows(self) -> None:
        orig = platform.system
        try:
            platform.system = lambda: "Windows"  # type: ignore[assignment]
            drv = host_status.default_drive()
            assert drv.endswith("\\")
        finally:
            platform.system = orig  # type: ignore[assignment]

    def test_default_drive_posix(self) -> None:
        orig = platform.system
        try:
            platform.system = lambda: "Linux"  # type: ignore[assignment]
            drv = host_status.default_drive()
            assert drv == "/"
        finally:
            platform.system = orig  # type: ignore[assignment]

    def test_get_disk_path_formatting_windows(self) -> None:
        orig_sys = platform.system
        orig_usage = host_status.shutil.disk_usage
        try:
            platform.system = lambda: "Windows"  # type: ignore[assignment]
            captured_paths: list[str] = []

            def fake_usage(path: str) -> Any:
                captured_paths.append(path)
                return host_status.shutil._ntuple_diskusage(100 * (1024**3), 60 * (1024**3), 40 * (1024**3))

            host_status.shutil.disk_usage = fake_usage  # type: ignore[assignment]
            host_status.get_disk("D:")
            assert captured_paths[-1] == "D:\\"
        finally:
            platform.system = orig_sys  # type: ignore[assignment]
            host_status.shutil.disk_usage = orig_usage  # type: ignore[assignment]

    def test_get_disk_path_formatting_posix(self) -> None:
        orig_sys = platform.system
        orig_usage = host_status.shutil.disk_usage
        try:
            platform.system = lambda: "Linux"  # type: ignore[assignment]
            captured_paths: list[str] = []

            def fake_usage(path: str) -> Any:
                captured_paths.append(path)
                return host_status.shutil._ntuple_diskusage(100 * (1024**3), 60 * (1024**3), 40 * (1024**3))

            host_status.shutil.disk_usage = fake_usage  # type: ignore[assignment]
            host_status.get_disk("/mnt/data")
            assert captured_paths[-1] == "/mnt/data"
        finally:
            platform.system = orig_sys  # type: ignore[assignment]
            host_status.shutil.disk_usage = orig_usage  # type: ignore[assignment]


# ---------- hermetic main integration ----------


class TestMainHermetic:
    """Hermetic tests for main() with mocked RAM and disk detection."""

    def _setup_mocks(
        self,
        ram_percent: float = 50.0,
        disk_free_gb: float = 100.0,
        drive: str = "C:\\",
    ) -> tuple[Any, Any]:
        orig_ram = host_status.get_ram
        orig_disk = host_status.get_disk

        host_status.get_ram = lambda: {  # type: ignore[assignment]
            "total_gb": 32.0,
            "used_gb": round(32.0 * (ram_percent / 100.0), 1),
            "available_gb": round(32.0 * (1.0 - ram_percent / 100.0), 1),
            "percent": ram_percent,
        }
        host_status.get_disk = lambda d=None: {  # type: ignore[assignment]
            "drive": d or drive,
            "total_gb": 500.0,
            "free_gb": disk_free_gb,
            "percent_used": round(((500.0 - disk_free_gb) / 500.0) * 100.0, 1),
        }
        return orig_ram, orig_disk

    def _restore_mocks(self, orig_ram: Any, orig_disk: Any) -> None:
        host_status.get_ram = orig_ram  # type: ignore[assignment]
        host_status.get_disk = orig_disk  # type: ignore[assignment]

    def test_main_json_ok(self, capsys: Any = None) -> None:
        orig_ram, orig_disk = self._setup_mocks(ram_percent=60.0, disk_free_gb=100.0)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                host_status.main(["--json"])
            data = json.loads(buf.getvalue())
            assert data["state"] == "ok"
            assert data["reasons"] == []
            assert data["ram"]["percent"] == 60.0
            assert data["disk"]["free_gb"] == 100.0
        finally:
            self._restore_mocks(orig_ram, orig_disk)

    def test_main_json_wait(self, capsys: Any = None) -> None:
        orig_ram, orig_disk = self._setup_mocks(ram_percent=92.0, disk_free_gb=100.0)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                host_status.main(["--json"])
            data = json.loads(buf.getvalue())
            assert data["state"] == "wait"
            assert "RAM >= 90%" in data["reasons"]
        finally:
            self._restore_mocks(orig_ram, orig_disk)

    def test_main_json_reap(self, capsys: Any = None) -> None:
        orig_ram, orig_disk = self._setup_mocks(ram_percent=86.0, disk_free_gb=100.0)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                host_status.main(["--json"])
            data = json.loads(buf.getvalue())
            assert data["state"] == "reap"
            assert "RAM >= 85%" in data["reasons"]
        finally:
            self._restore_mocks(orig_ram, orig_disk)

    def test_main_json_no_spawn(self, capsys: Any = None) -> None:
        orig_ram, orig_disk = self._setup_mocks(ram_percent=96.0, disk_free_gb=15.0)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                host_status.main(["--json"])
            data = json.loads(buf.getvalue())
            assert data["state"] == "no_spawn"
            assert "RAM >= 95%" in data["reasons"]
            assert "disk < 20 GB" in data["reasons"]
        finally:
            self._restore_mocks(orig_ram, orig_disk)

    def test_main_human(self, capsys: Any = None) -> None:
        orig_ram, orig_disk = self._setup_mocks(ram_percent=50.0, disk_free_gb=100.0)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                host_status.main([])
            out = buf.getvalue()
            assert "RAM:" in out
            assert "Disk:" in out
            assert "State: ok" in out
        finally:
            self._restore_mocks(orig_ram, orig_disk)

    def test_main_unknown_flag_exits(self) -> None:
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            try:
                host_status.main(["--invalid-flag"])
                assert False, "Should have exited with SystemExit"
            except SystemExit as exc:
                assert exc.code == 2

    def test_main_missing_drive_value_exits(self) -> None:
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            try:
                host_status.main(["--drive"])
                assert False, "Should have exited with SystemExit"
            except SystemExit as exc:
                assert exc.code == 2

    def test_main_nonexistent_drive_exits(self) -> None:
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            try:
                host_status.main(["--drive", "NONEXISTENT_DRIVE_XZY:/bogus/path"])
                assert False, "Should have exited with SystemExit"
            except SystemExit as exc:
                assert exc.code == 2
        assert "host_status.py: error:" in err_buf.getvalue()

def _run() -> int:
    """Standalone runner for CI environments without pytest."""
    classes = [
        TestClassify(),
        TestFormatHuman(),
        TestFormatJson(),
        TestPortability(),
        TestMainHermetic(),
    ]
    passed = 0
    failed = 0
    for cls_instance in classes:
        cls_name = cls_instance.__class__.__name__
        for name in dir(cls_instance):
            if name.startswith("test_"):
                test_fn = getattr(cls_instance, name)
                if callable(test_fn):
                    try:
                        test_fn()
                        passed += 1
                    except Exception as e:
                        print(f"FAIL: {cls_name}.{name}: {e}", file=sys.stderr)
                        failed += 1

    print(f"test_host_status: {passed} passed, {failed} failed")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(_run())
