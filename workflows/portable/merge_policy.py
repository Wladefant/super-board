#!/usr/bin/env python3
"""merge_policy.py - Shared policy switch for temporary merge-first workflow.

Provides `merge_first_enabled(env=None)`:
  - Defaults to ON (True) unless SUPERBOARD_MERGE_FIRST is set to 0, false, no, or off.
  - Setting SUPERBOARD_MERGE_FIRST=0 restores the legacy pre-merge QA gate order.
  - Risk-based review requirements and production exclusions remain strictly preserved.
"""
import os
from typing import Mapping, Optional

MERGE_FIRST_ENV_VAR = "SUPERBOARD_MERGE_FIRST"
_FALSEY_VALUES = frozenset({"0", "false", "no", "off"})


def merge_first_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return whether temporary merge-first behavior is enabled.

    Default is ON (True) unless SUPERBOARD_MERGE_FIRST is explicitly set to
    a falsey value ('0', 'false', 'no', 'off').
    """
    source = os.environ if env is None else env
    val = source.get(MERGE_FIRST_ENV_VAR)
    if val is None:
        return True
    return str(val).strip().lower() not in _FALSEY_VALUES
