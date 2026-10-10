#!/usr/bin/env python3
"""test_merge_policy.py - Unit tests for shared merge-first policy switch.

Covers:
  - Default ON when SUPERBOARD_MERGE_FIRST is unset
  - SUPERBOARD_MERGE_FIRST=0 restoring old behavior (returns False)
  - Values 'false', 'no', 'off' restoring old behavior
  - Values '1', 'true', 'yes', 'on' keeping merge-first enabled
  - Custom mapping passed via env parameter
"""
import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from merge_policy import merge_first_enabled, MERGE_FIRST_ENV_VAR


class TestMergePolicy(unittest.TestCase):
    def setUp(self):
        self._orig_val = os.environ.get(MERGE_FIRST_ENV_VAR)

    def tearDown(self):
        if self._orig_val is not None:
            os.environ[MERGE_FIRST_ENV_VAR] = self._orig_val
        else:
            os.environ.pop(MERGE_FIRST_ENV_VAR, None)

    def test_default_on_when_unset(self):
        os.environ.pop(MERGE_FIRST_ENV_VAR, None)
        self.assertTrue(merge_first_enabled())

    def test_explicit_zero_restores_legacy(self):
        os.environ[MERGE_FIRST_ENV_VAR] = "0"
        self.assertFalse(merge_first_enabled())

    def test_explicit_falsey_values(self):
        for val in ("0", "false", "no", "off", "FALSE", "Off", " 0 "):
            os.environ[MERGE_FIRST_ENV_VAR] = val
            self.assertFalse(merge_first_enabled(), f"Expected False for {val!r}")

    def test_explicit_truthy_values(self):
        for val in ("1", "true", "yes", "on", "TRUE", " 1 "):
            os.environ[MERGE_FIRST_ENV_VAR] = val
            self.assertTrue(merge_first_enabled(), f"Expected True for {val!r}")

    def test_custom_env_mapping(self):
        self.assertFalse(merge_first_enabled({MERGE_FIRST_ENV_VAR: "0"}))
        self.assertTrue(merge_first_enabled({MERGE_FIRST_ENV_VAR: "1"}))
        self.assertTrue(merge_first_enabled({}))


if __name__ == "__main__":
    unittest.main()
