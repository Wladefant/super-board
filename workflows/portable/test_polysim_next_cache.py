#!/usr/bin/env python3
"""Tests for polysim_next_cache.py: links are removed without touching their target, only
webpack caches holding another worktree's modules are dropped, and .tsbuildinfo is seeded
only from a checked source with the same lockfile, nearest commit first."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import polysim_next_cache as nc  # noqa: E402  (path set up above)


def make_dir_link(target: Path, link: Path) -> None:
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        os.symlink(target, link, target_is_directory=True)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()


def write_pack(webpack: Path, *worktrees: str) -> None:
    (webpack / "client-production").mkdir(parents=True)
    body = b"".join(f"C:\\dev\\{w}\\frontend\\src\\page.tsx|".encode() for w in worktrees)
    (webpack / "client-production" / "index.pack").write_bytes(b"\x00pack" + body)


class CacheDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wt = self.tmp / "wt-a"
        (self.wt / "frontend" / ".next").mkdir(parents=True)

    def test_linked_cache_is_unlinked_and_target_keeps_its_files(self) -> None:
        store = self.tmp / "store"
        (store / "webpack").mkdir(parents=True)
        (store / "webpack" / "keep.pack").write_bytes(b"x" * 10)
        make_dir_link(store, nc.cache_dir(self.wt))
        self.assertTrue(nc.wtr.is_link(nc.cache_dir(self.wt)))

        result = nc.own_cache_dir(self.wt)

        self.assertEqual(result["cache"], "unlinked")
        self.assertEqual(result["was_linked_to"], nc.wtr.norm(store))
        self.assertFalse(nc.wtr.is_link(nc.cache_dir(self.wt)))
        self.assertEqual(list(nc.cache_dir(self.wt).iterdir()), [])
        self.assertEqual((store / "webpack" / "keep.pack").read_bytes(), b"x" * 10)

    def test_webpack_cache_of_own_worktree_is_kept(self) -> None:
        webpack = nc.cache_dir(self.wt) / "webpack"
        write_pack(webpack, "wt-a")
        self.assertEqual(nc.drop_foreign_webpack(self.wt)["webpack"], "own")
        self.assertTrue(webpack.is_dir())

    def test_webpack_cache_naming_another_worktree_is_dropped(self) -> None:
        webpack = nc.cache_dir(self.wt) / "webpack"
        write_pack(webpack, "wt-a", ".wt-review-5576")
        result = nc.drop_foreign_webpack(self.wt)
        self.assertEqual(result["webpack"], "dropped")
        self.assertEqual(result["webpack_worktrees"], [".wt-review-5576", "wt-a"])
        self.assertFalse(webpack.exists())

    def test_webpack_behind_a_link_is_never_deleted(self) -> None:
        store = self.tmp / "store"
        write_pack(store / "webpack", "someone-else")
        make_dir_link(store, nc.cache_dir(self.wt))
        self.assertEqual(nc.drop_foreign_webpack(self.wt)["webpack"], "none")
        self.assertTrue((store / "webpack" / "client-production" / "index.pack").is_file())


class SeedTests(unittest.TestCase):
    """A clone with worktrees at different commits and lockfiles."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clone = self.tmp / "clone"
        (self.clone / "frontend").mkdir(parents=True)
        git(self.clone, "init", "-q")
        git(self.clone, "config", "user.email", "t@example.com")
        git(self.clone, "config", "user.name", "t")
        self.lock = self.clone / "frontend" / "package-lock.json"
        self.lock.write_text('{"v": 1}\n', encoding="utf-8")
        self.commits = []
        for i in range(4):
            (self.clone / "frontend" / "f.ts").write_text(f"export const n = {i};\n", encoding="utf-8")
            git(self.clone, "add", "-A")
            git(self.clone, "commit", "-qm", f"c{i}")
            self.commits.append(git(self.clone, "rev-parse", "HEAD"))

    def worktree(self, name: str, commit: str, lock: str | None = None, prepared: bool = True) -> Path:
        wt = self.tmp / name
        git(self.clone, "worktree", "add", "-q", "--detach", str(wt), commit)
        if lock is not None:
            (wt / "frontend" / "package-lock.json").write_text(lock, encoding="utf-8")
        cache = nc.cache_dir(wt)
        cache.mkdir(parents=True)
        (cache / nc.TSBUILDINFO).write_text(name, encoding="utf-8")
        if prepared:
            marker = {"head": commit, "lock_key": nc.lock_key(wt)}
            (cache / nc.WARM_MARKER).write_text(json.dumps(marker), encoding="utf-8")
        return wt

    def test_nearest_checked_source_with_same_lockfile_is_seeded(self) -> None:
        self.worktree("far", self.commits[0])
        self.worktree("near", self.commits[2])
        self.worktree("unchecked", self.commits[3], prepared=False)
        self.worktree("other-lock", self.commits[3], lock='{"v": 2}\n')
        target = self.tmp / "target"
        git(self.clone, "worktree", "add", "-q", "--detach", str(target), self.commits[3])
        nc.cache_dir(target).mkdir(parents=True)

        ranked = [(Path(r["worktree"]).name, r["distance"]) for r in nc.rank_sources(target, self.clone)]
        self.assertEqual(ranked, [("near", 1), ("far", 3)])  # never unchecked or other-lock

        result = nc.seed_tsbuildinfo(target, self.clone, None)
        self.assertEqual((result["tsbuildinfo"], result["distance"]), ("seeded", 1))
        self.assertEqual((nc.cache_dir(target) / nc.TSBUILDINFO).read_text(encoding="utf-8"), "near")

    def test_checked_own_buildinfo_is_not_replaced(self) -> None:
        self.worktree("near", self.commits[2])
        own = self.worktree("own", self.commits[3])
        self.assertEqual(nc.seed_tsbuildinfo(own, self.clone, None), {"tsbuildinfo": "own"})
        self.assertEqual((nc.cache_dir(own) / nc.TSBUILDINFO).read_text(encoding="utf-8"), "own")

    def test_unchecked_own_buildinfo_is_kept_when_no_source_exists(self) -> None:
        own = self.worktree("own", self.commits[3], prepared=False)
        self.assertEqual(nc.seed_tsbuildinfo(own, self.clone, None), {"tsbuildinfo": "kept unchecked"})


class SweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clone = self.tmp / "clone"
        (self.clone / "frontend").mkdir(parents=True)
        git(self.clone, "init", "-q")
        git(self.clone, "config", "user.email", "t@example.com")
        git(self.clone, "config", "user.name", "t")
        (self.clone / "frontend" / "f.ts").write_text("export const x = 1;\n", encoding="utf-8")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-qm", "c0")
        self.store = self.tmp / "store"
        (self.store / "webpack").mkdir(parents=True)
        (self.store / "webpack" / "pack.pack").write_bytes(b"x" * 100)

    def worktree(self, name: str) -> Path:
        wt = self.tmp / name
        git(self.clone, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
        (wt / "frontend" / ".next").mkdir(parents=True, exist_ok=True)
        return wt

    def test_sweep_refuses_apply_without_confirm(self) -> None:
        rc = nc.main(["--clone", str(self.clone), "sweep", "--store", str(self.store), "--apply"])
        self.assertEqual(rc, 2)
        # Store is untouched
        self.assertTrue((self.store / "webpack" / "pack.pack").is_file())

    def test_sweep_dry_run_reports_without_modifying(self) -> None:
        wt = self.worktree("wt-idle")
        make_dir_link(self.store, nc.cache_dir(wt))
        rc = nc.main(["--clone", str(self.clone), "sweep", "--store", str(self.store)])
        self.assertEqual(rc, 0)
        # Still linked
        self.assertTrue(nc.wtr.is_link(nc.cache_dir(wt)))
        self.assertTrue((self.store / "webpack" / "pack.pack").is_file())

    def test_sweep_apply_with_confirm_unlinks_idle_and_deletes_unlinked_store(self) -> None:
        wt = self.worktree("wt-idle")
        make_dir_link(self.store, nc.cache_dir(wt))
        rc = nc.main(["--clone", str(self.clone), "sweep", "--store", str(self.store), "--apply", "--confirm"])
        self.assertEqual(rc, 0)
        # Worktree is unlinked
        self.assertFalse(nc.wtr.is_link(nc.cache_dir(wt)))
        # Store webpack directory is deleted since no busy worktree linked to it
        self.assertFalse((self.store / "webpack").exists())

    def test_sweep_unreadable_link_does_not_crash(self) -> None:
        wt = self.worktree("wt-badlink")
        make_dir_link(self.store, nc.cache_dir(wt))
        orig_target = nc.wtr.link_target
        try:
            nc.wtr.link_target = lambda p: None
            rc = nc.main(["--clone", str(self.clone), "sweep", "--store", str(self.store)])
            self.assertEqual(rc, 0)
        finally:
            nc.wtr.link_target = orig_target

    def test_sweep_subcommand_accepts_clone_flag(self) -> None:
        rc = nc.main(["sweep", "--clone", str(self.clone), "--store", str(self.store)])
        self.assertEqual(rc, 0)

if __name__ == "__main__":
    unittest.main()
