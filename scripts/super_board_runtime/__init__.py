"""Shared Superboard runtime.

Every policy decision the Superboard pipeline makes lives in this package so the
same decision cannot diverge between Bash, Python, and JavaScript. The shell
entry points and the Node workflow keep their names and delegate here.

Python 3.11+, standard library only. No third-party dependencies, ever.

Exit-code contract (every runtime CLI writes machine-readable JSON to stdout and
human diagnostics to stderr):

  0   success
  3   compare-before-mutate conflict; nothing was changed
  64  invalid command invocation
  65  invalid configuration or input contract
  69  authentication, identity, or permission failure
  75  quota unavailable or the immutable GraphQL reserve was reached
  78  unsafe evidence rejected at the publication boundary
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_CONFLICT = 3
EXIT_USAGE = 64
EXIT_CONFIG = 65
EXIT_AUTH = 69
EXIT_QUOTA = 75
EXIT_UNSAFE = 78

__all__ = [
    "EXIT_OK",
    "EXIT_CONFLICT",
    "EXIT_USAGE",
    "EXIT_CONFIG",
    "EXIT_AUTH",
    "EXIT_QUOTA",
    "EXIT_UNSAFE",
]
