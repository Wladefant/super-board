"""The canonical seven-state Superboard lifecycle contract.

There is exactly one lifecycle, it is not configurable, and `Skipped` is not
part of it. Boards that still carry a `Skipped` option must be reconciled; the
runtime refuses to treat it as a status rather than silently mapping it onto
something else.
"""

from __future__ import annotations

LIFECYCLE_STATUSES: tuple[str, ...] = (
    "Backlog",
    "Ready",
    "Building",
    "QA",
    "Review",
    "Blocked",
    "Done",
)

#: The only status a card may hold to be dispatched. There is no
#: "eligible for the requested lane" concept.
DISPATCHABLE_STATUS = "Ready"

#: Statuses that used to exist and are now refused outright.
RETIRED_STATUSES: tuple[str, ...] = ("Skipped",)


class LifecycleError(ValueError):
    """A value was offered where a lifecycle status was expected, and failed."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


_CANONICAL_BY_FOLDED = {status.casefold(): status for status in LIFECYCLE_STATUSES}
_RETIRED_FOLDED = frozenset(status.casefold() for status in RETIRED_STATUSES)


def canonicalize_status(value: str) -> str:
    """Return the canonical spelling of ``value``.

    Trims surrounding whitespace and compares case-insensitively. Raises
    :class:`LifecycleError` for ``Skipped`` and for anything unrecognised —
    never guesses, never falls back to a permissive default.
    """
    if not isinstance(value, str):
        raise LifecycleError(
            "lifecycle-status-invalid",
            f"lifecycle status must be a string, got {type(value).__name__}",
        )
    trimmed = value.strip()
    if not trimmed:
        raise LifecycleError("lifecycle-status-invalid", "lifecycle status must not be empty")
    folded = trimmed.casefold()
    if folded in _RETIRED_FOLDED:
        raise LifecycleError(
            "lifecycle-status-skipped",
            "'Skipped' is not a Superboard lifecycle status; the canonical statuses are "
            + ", ".join(LIFECYCLE_STATUSES),
        )
    canonical = _CANONICAL_BY_FOLDED.get(folded)
    if canonical is None:
        raise LifecycleError(
            "lifecycle-status-unknown",
            f"unknown lifecycle status: {trimmed!r}; the canonical statuses are "
            + ", ".join(LIFECYCLE_STATUSES),
        )
    return canonical


def is_dispatchable_status(value: object) -> bool:
    """True only when ``value`` canonicalizes to exactly ``Ready``."""
    if not isinstance(value, str):
        return False
    try:
        return canonicalize_status(value) == DISPATCHABLE_STATUS
    except LifecycleError:
        return False


__all__ = [
    "DISPATCHABLE_STATUS",
    "LIFECYCLE_STATUSES",
    "RETIRED_STATUSES",
    "LifecycleError",
    "canonicalize_status",
    "is_dispatchable_status",
]
