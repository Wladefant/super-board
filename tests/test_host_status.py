"""Tests for host_status.py — classify, format, and edge cases."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Import the module under test from workflows/portable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflows" / "portable"))
import host_status


# ---------- classify ----------


class TestClassify:
    """Threshold-based state classification."""

    def test_ok_normal_load(self):
        state, reasons = host_status.classify(ram_percent=60.0, disk_free_gb=100.0)
        assert state == "ok"
        assert reasons == []

    def test_ok_just_below_reap_thresholds(self):
        state, reasons = host_status.classify(ram_percent=84.9, disk_free_gb=25.0)
        assert state == "ok"
        assert reasons == []

    def test_reap_ram_at_boundary(self):
        state, reasons = host_status.classify(ram_percent=85.0, disk_free_gb=100.0)
        assert state == "reap"
        assert "RAM >= 85%" in reasons

    def test_reap_disk_at_boundary(self):
        state, reasons = host_status.classify(ram_percent=50.0, disk_free_gb=24.9)
        assert state == "reap"
        assert "disk < 25 GB" in reasons

    def test_reap_both_triggers(self):
        state, reasons = host_status.classify(ram_percent=87.0, disk_free_gb=20.0)
        assert state == "reap"
        assert len(reasons) == 2

    def test_no_spawn_ram_at_boundary(self):
        state, reasons = host_status.classify(ram_percent=95.0, disk_free_gb=100.0)
        assert state == "no_spawn"
        assert "RAM >= 95%" in reasons

    def test_no_spawn_disk_at_boundary(self):
        state, reasons = host_status.classify(ram_percent=50.0, disk_free_gb=9.9)
        assert state == "no_spawn"
        assert "disk < 10 GB" in reasons

    def test_no_spawn_both_triggers(self):
        state, reasons = host_status.classify(ram_percent=96.0, disk_free_gb=5.0)
        assert state == "no_spawn"
        assert len(reasons) == 2

    def test_no_spawn_trumps_reap(self):
        """When both no_spawn and reap thresholds are hit, no_spawn wins."""
        state, _ = host_status.classify(ram_percent=97.0, disk_free_gb=20.0)
        assert state == "no_spawn"

    def test_zero_ram(self):
        state, _ = host_status.classify(ram_percent=0.0, disk_free_gb=500.0)
        assert state == "ok"

    def test_hundred_percent_ram(self):
        state, _ = host_status.classify(ram_percent=100.0, disk_free_gb=500.0)
        assert state == "no_spawn"

    def test_zero_disk(self):
        state, _ = host_status.classify(ram_percent=50.0, disk_free_gb=0.0)
        assert state == "no_spawn"


# ---------- format_human ----------


class TestFormatHuman:
    def test_ok_output(self):
        ram = {"total_gb": 27.9, "used_gb": 16.0, "available_gb": 11.9, "percent": 57.3}
        disk = {"drive": "C:\\", "total_gb": 1863.0, "free_gb": 500.0, "percent_used": 73.2}
        output = host_status.format_human(ram, disk, "ok", [])
        assert "RAM:" in output
        assert "Disk:" in output
        assert "State: ok" in output

    def test_reap_output_shows_reason(self):
        ram = {"total_gb": 27.9, "used_gb": 24.0, "available_gb": 3.9, "percent": 86.0}
        disk = {"drive": "C:\\", "total_gb": 1863.0, "free_gb": 500.0, "percent_used": 73.2}
        output = host_status.format_human(ram, disk, "reap", ["RAM >= 85%"])
        assert "reap" in output
        assert "RAM >= 85%" in output


# ---------- format_json ----------


class TestFormatJson:
    def test_valid_json(self):
        ram = {"total_gb": 27.9, "used_gb": 16.0, "available_gb": 11.9, "percent": 57.3}
        disk = {"drive": "C:\\", "total_gb": 1863.0, "free_gb": 500.0, "percent_used": 73.2}
        output = host_status.format_json(ram, disk, "ok", [])
        parsed = json.loads(output)
        assert parsed["state"] == "ok"
        assert parsed["ram"]["total_gb"] == 27.9
        assert parsed["disk"]["drive"] == "C:\\"
        assert parsed["reasons"] == []

    def test_json_with_reasons(self):
        ram = {"total_gb": 27.9, "used_gb": 26.5, "available_gb": 1.4, "percent": 95.0}
        disk = {"drive": "C:\\", "total_gb": 1863.0, "free_gb": 8.0, "percent_used": 99.6}
        output = host_status.format_json(ram, disk, "no_spawn", ["RAM >= 95%", "disk < 10 GB"])
        parsed = json.loads(output)
        assert parsed["state"] == "no_spawn"
        assert len(parsed["reasons"]) == 2


# ---------- main (integration) ----------


class TestMain:
    def test_main_json(self, capsys):
        host_status.main(["--json"])
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert "ram" in parsed
        assert "disk" in parsed
        assert parsed["state"] in ("ok", "reap", "no_spawn")

    def test_main_human(self, capsys):
        host_status.main([])
        captured = capsys.readouterr()
        assert "RAM:" in captured.out
        assert "Disk:" in captured.out
        assert "State:" in captured.out
