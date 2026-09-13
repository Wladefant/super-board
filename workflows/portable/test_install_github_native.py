"""Installer contract: exact parity, missing-source preflight, no state mutation."""
import tempfile
import json
import unittest
from pathlib import Path
from install_github_native import POLICY, RUNTIME_FILES, synchronize


class Installation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source, self.runtime, self.profile = root / "source", root / "runtime", root / "profile/AGENTS.md"
        for path in [POLICY] + [Path("workflows/portable") / name for name in RUNTIME_FILES]:
            target = self.source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(("Reviewable source: " + str(path) + "\r\n").encode())
        self.runtime.mkdir()
        (self.runtime / "state.json").write_bytes(b"operator state")
        self.source_manifest = {"authority": {"shared_system_of_record": "GitHub"}, "modules": {
            name: {"role": name} for name in ("github_work_item.py", "review_content.py", "install_github_native.py", "ledger.py")
        }}
        (self.source / "workflows/portable/manifest.json").write_text(json.dumps(self.source_manifest))
        (self.runtime / "manifest.json").write_text(json.dumps({"unrelated": "preserved", "modules": {"peer.py": {"role": "peer"}}}))

    def test_exact_install_idempotence_and_drift_detection(self):
        self.assertTrue(synchronize(self.source, self.profile, self.runtime))
        self.assertTrue(synchronize(self.source, self.profile, self.runtime, check=True))
        self.assertEqual(self.profile.read_bytes(), (self.source / POLICY).read_bytes())
        self.assertTrue(synchronize(self.source, self.profile, self.runtime))
        self.assertEqual((self.runtime / "state.json").read_bytes(), b"operator state")
        manifest = json.loads((self.runtime / "manifest.json").read_text())
        self.assertEqual(manifest["unrelated"], "preserved")
        self.assertEqual(manifest["modules"]["peer.py"], {"role": "peer"})
        self.assertEqual(manifest["authority"], self.source_manifest["authority"])
        self.profile.write_bytes(b"unreviewed edit")
        self.assertFalse(synchronize(self.source, self.profile, self.runtime, check=True))
        self.assertEqual(self.profile.read_bytes(), b"unreviewed edit")

    def test_missing_source_never_partially_installs(self):
        (self.source / "workflows/portable" / RUNTIME_FILES[-1]).unlink()
        with self.assertRaises(FileNotFoundError):
            synchronize(self.source, self.profile, self.runtime)
        self.assertFalse(self.profile.exists())
        self.assertEqual({p.name for p in self.runtime.iterdir()}, {"state.json", "manifest.json"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
