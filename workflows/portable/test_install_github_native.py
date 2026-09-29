"""Installer contract: exact parity, missing-source preflight, no state mutation."""
import ast
import contextlib
import hashlib
import io
import tempfile
import json
import unittest
from pathlib import Path
from install_github_native import POLICY, PROFILE_EXTENSIONS, RUNTIME_FILES, policy_state_path, synchronize


class Installation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source, self.runtime, self.profile = root / "source", root / "runtime", root / "profile/AGENTS.md"
        for path in [POLICY] + [Path("workflows/portable") / name for name in RUNTIME_FILES] + [
                    Path("workflows/portable/extensions") / name for name in PROFILE_EXTENSIONS
                ]:
            target = self.source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(("Reviewable source: " + str(path) + "\r\n").encode())
        self.runtime.mkdir()
        (self.runtime / "state.json").write_bytes(b"operator state")
        self.source_manifest = {"authority": {"shared_system_of_record": "GitHub"}, "modules": {
            name: {"role": name} for name in (
                "github_work_item.py", "review_content.py", "install_github_native.py", "ledger.py", "verify.py",
                "model_routing.py", "balance_loader.py", "routing_smoke_test.py",
            )
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

    def test_live_policy_edits_are_refused_not_reverted(self):
        """Negative control: the live profile keeps its rulings; the install refuses and explains."""
        self.assertTrue(synchronize(self.source, self.profile, self.runtime))
        expected = hashlib.sha256((self.source / POLICY).read_bytes()).hexdigest()
        self.assertEqual(policy_state_path(self.profile).read_text(encoding="utf-8").strip(), expected)
        self.profile.write_bytes(b"operator ruling added after the install\r\n")
        report = io.StringIO()
        with contextlib.redirect_stdout(report):
            self.assertFalse(synchronize(self.source, self.profile, self.runtime))
        self.assertIn("REFUSED", report.getvalue())
        self.assertIn("-operator ruling added after the install", report.getvalue())
        self.assertEqual(self.profile.read_bytes(), b"operator ruling added after the install\r\n")
        self.assertEqual((self.runtime / "state.json").read_bytes(), b"operator state")
        # --force-policy overwrites deliberately, and re-records the installed hash.
        self.assertTrue(synchronize(self.source, self.profile, self.runtime, force_policy=True))
        self.assertEqual(self.profile.read_bytes(), (self.source / POLICY).read_bytes())
        self.assertEqual(policy_state_path(self.profile).read_text(encoding="utf-8").strip(), expected)
        # A read-only check reports the drift, refuses nothing and writes nothing.
        self.profile.write_bytes(b"second live ruling\r\n")
        report = io.StringIO()
        with contextlib.redirect_stdout(report):
            self.assertFalse(synchronize(self.source, self.profile, self.runtime, check=True))
        self.assertIn(f"DRIFT: {self.profile.name}", report.getvalue())
        self.assertEqual(self.profile.read_bytes(), b"second live ruling\r\n")
        # A live profile already holding the repository text installs without a refusal.
        self.profile.write_bytes((self.source / POLICY).read_bytes())
        self.assertTrue(synchronize(self.source, self.profile, self.runtime))

    def test_missing_source_never_partially_installs(self):
        (self.source / "workflows/portable" / RUNTIME_FILES[-1]).unlink()
        with self.assertRaises(FileNotFoundError):
            synchronize(self.source, self.profile, self.runtime)
        self.assertFalse(self.profile.exists())
        self.assertEqual({p.name for p in self.runtime.iterdir()}, {"state.json", "manifest.json"})

    def test_router_modules_are_installed_and_drift_checked(self):
        self.assertTrue(synchronize(self.source, self.profile, self.runtime))
        for name in ("model_routing.py", "balance_loader.py", "routing_smoke_test.py"):
            with self.subTest(name=name):
                installed = self.runtime / name
                self.assertEqual(installed.read_bytes(), (self.source / "workflows/portable" / name).read_bytes())
                installed.write_bytes(b"stale router")
                report = io.StringIO()
                with contextlib.redirect_stdout(report):
                    self.assertFalse(synchronize(self.source, self.profile, self.runtime, check=True))
                self.assertIn(f"DRIFT: {name}", report.getvalue())
                self.assertEqual(installed.read_bytes(), b"stale router")
                self.assertTrue(synchronize(self.source, self.profile, self.runtime))

    def test_router_modules_deploy_through_manifest(self):
        self.assertTrue(synchronize(self.source, self.profile, self.runtime))
        manifest = json.loads((self.runtime / "manifest.json").read_text(encoding="utf-8"))
        for name in ("model_routing.py", "balance_loader.py", "routing_smoke_test.py"):
            with self.subTest(name=name):
                self.assertIn(name, manifest.get("modules", {}), f"{name} not deployed in runtime manifest modules")
                self.assertEqual(manifest["modules"][name], {"role": name})
        required = manifest.get("export", {}).get("required_files", [])
        for name in ("model_routing.py", "balance_loader.py", "routing_smoke_test.py"):
            with self.subTest(required_name=name):
                self.assertIn(name, required, f"{name} not in runtime manifest required_files")

    def test_profile_extensions_land_beside_the_profile_and_are_drift_checked(self):
        self.assertTrue(synchronize(self.source, self.profile, self.runtime))
        for name in PROFILE_EXTENSIONS:
            with self.subTest(name=name):
                installed = self.profile.parent / "extensions" / name
                source = self.source / "workflows/portable/extensions" / name
                self.assertEqual(installed.read_bytes(), source.read_bytes())
                self.assertFalse((self.runtime / name).exists())
                installed.write_bytes(b"stale hook")
                report = io.StringIO()
                with contextlib.redirect_stdout(report):
                    self.assertFalse(synchronize(self.source, self.profile, self.runtime, check=True))
                self.assertIn(f"DRIFT: {name}", report.getvalue())

    def test_router_import_closure_is_installed(self):
        # A routing module the installed router imports but the installer skips leaves the
        # runtime running a stale copy while --check still reports MATCH.
        portable = Path(__file__).resolve().parent
        local = {path.stem for path in portable.glob("*.py")}
        closure, pending = set(), ["routing_smoke_test", "model_routing"]
        while pending:
            module = pending.pop()
            if module in closure:
                continue
            closure.add(module)
            for node in ast.walk(ast.parse((portable / f"{module}.py").read_text(encoding="utf-8"))):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else (
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                pending.extend(name.split(".")[0] for name in names if name.split(".")[0] in local)
        self.assertEqual({f"{module}.py" for module in closure} - set(RUNTIME_FILES), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
