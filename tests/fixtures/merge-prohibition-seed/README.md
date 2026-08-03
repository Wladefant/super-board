# Seeded merge-prohibition fixture — NOT an active path

Every file in this directory deliberately contains a merge mechanism so
`scan_merge_prohibitions` can be proven to detect it. Nothing here executes.

Every line here is an unnegated instruction, so the scanner's
prohibition-statement rule does not apply to it and must not: these files exist
to be caught. They are excluded from the real-tree scan by an explicit entry in
the repository-root allowlist file, never by a path heuristic.
