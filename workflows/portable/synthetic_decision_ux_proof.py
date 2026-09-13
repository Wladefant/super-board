#!/usr/bin/env python3
"""Run the explicitly synthetic, local decision UX fixture proofs.

This script does not contact GitHub, does not claim a human approval, and does not
activate any workflow. Live GitHub transport remains a separate deployment gate.
"""

import unittest

from test_decision_ux_adapter import DecisionUXProof


def run_synthetic_proof() -> bool:
    print("SYNTHETIC FIXTURE PROOF: GitHub decision UX (no live approval)")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DecisionUXProof)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(0 if run_synthetic_proof() else 1)
