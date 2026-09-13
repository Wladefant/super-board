import os
import subprocess
import tempfile
import unittest
from review_content import evaluate, git, content_identity, json_pages, target_shas


class ContentReviews(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = self.temp.name
        self.g('init', '-b', 'staging')
        self.g('config', 'user.name', 'Wladimir Kirjanovs')
        self.g('config', 'user.email', 'wladefant@gmail.com')
        self.commit('app', 'original\n')
        self.base = self.g('rev-parse', 'HEAD')
        self.g('update-ref', 'refs/remotes/origin/staging', self.base)
        self.g('checkout', '-b', 'feature')
        self.commit('app', 'feature\n')
        self.reviewed = self.g('rev-parse', 'HEAD')

    def g(self, *args):
        return git(*args, cwd=self.cwd)

    def commit(self, path, text):
        with open(os.path.join(self.cwd, path), 'w') as f:
            f.write(text)
        self.g('add', path)
        self.g('commit', '-m', path)

    def review(self, sha, delta=None, actor='reviewer'):
        return {'state': 'COMMENTED', 'user': {'login': actor},
                'body': 'APPROVE ' + sha + (('\ndelta-from: ' + delta) if delta else '')}

    def check(self, reviews, staging=True):
        return evaluate(reviews, self.g('rev-parse', 'HEAD'), 'author', cwd=self.cwd, staging=staging)

    def sync(self, conflict=False):
        self.g('checkout', 'staging')
        self.commit('app' if conflict else 'other', 'base advancement\n')
        self.g('update-ref', 'refs/remotes/origin/staging', self.g('rev-parse', 'HEAD'))
        self.g('checkout', 'feature')
        result = subprocess.run(['git', 'merge', '--no-ff', 'origin/staging', '-m', 'sync'], cwd=self.cwd, capture_output=True)
        if conflict:
            self.assertNotEqual(result.returncode, 0)
            self.commit('app', 'manual resolution\n')
        else:
            self.assertEqual(result.returncode, 0)

    def test_sync_without_conflicts_keeps_freshness(self):
        self.sync()
        self.assertTrue(self.check([self.review(self.reviewed)])['passed'])

    def test_conflict_resolution_breaks_freshness(self):
        self.sync(True)
        self.assertFalse(self.check([self.review(self.reviewed)])['passed'])

    def test_fix_commit_breaks_freshness(self):
        self.commit('app', 'fixed\n')
        self.assertFalse(self.check([self.review(self.reviewed)])['passed'])

    def test_delta_chain_restores_freshness(self):
        self.commit('app', 'fixed\n')
        fixed = self.g('rev-parse', 'HEAD')
        self.commit('app', 'fixed again\n')
        head = self.g('rev-parse', 'HEAD')
        reviews = [self.review(self.reviewed), self.review(fixed, self.reviewed), self.review(head, fixed)]
        self.assertTrue(self.check(reviews)['passed'])
        self.assertFalse(self.check(reviews[1:])['passed'])

    def test_delta_source_must_be_an_ancestor(self):
        # An approved sibling commit is a legitimate chain root everywhere except
        # here: it is not in this head's history, so it cannot vouch for its diff.
        # Same shape as test_delta_chain_restores_freshness; ancestry is the only
        # difference, so this isolates the ancestor branch. Assertions stay on
        # 'passed' because the installed-gate subclass reuses this fixture and
        # only surfaces that key.
        self.g('checkout', '-b', 'sibling', self.base)
        self.commit('other', 'sibling work\n')
        sibling = self.g('rev-parse', 'HEAD')
        self.g('checkout', 'feature')
        self.commit('app', 'fixed\n')
        head = self.g('rev-parse', 'HEAD')
        self.assertFalse(self.check([self.review(sibling), self.review(head, sibling)])['passed'])

    def test_every_delta_source_is_validated(self):
        # A body naming several sources binds to all of them; an unapproved one
        # must not be waved through because the first source happened to be valid.
        self.commit('app', 'fixed\n')
        fixed = self.g('rev-parse', 'HEAD')
        self.commit('app', 'fixed again\n')
        head = self.g('rev-parse', 'HEAD')
        chained = self.review(head, fixed)
        chained['body'] += '\ndelta-from: ' + self.base
        reviews = [self.review(self.reviewed), self.review(fixed, self.reviewed), chained]
        self.assertFalse(self.check(reviews)['passed'])
        self.assertTrue(self.check(reviews[:2] + [self.review(head, fixed)])['passed'])

    def test_author_self_review_rejected_outside_staging(self):
        review = self.review(self.reviewed, actor='author')
        review['state'] = 'APPROVED'
        self.assertFalse(self.check([review], staging=False)['passed'])

    def test_staging_automated_comment_waiver(self):
        self.assertTrue(self.check([self.review(self.reviewed, actor='author')])['passed'])

    def test_docs_change_is_full_diff_change(self):
        self.commit('notes.md', 'evidence\n')
        self.assertFalse(self.check([self.review(self.reviewed)])['passed'])

    def test_missing_review_fails(self):
        self.assertFalse(self.check([])['passed'])


    def test_negative_verdict_and_transitions(self):
        approval = self.review(self.reviewed)
        rejection = self.review(self.reviewed)
        rejection['body'] = 'REQUEST-CHANGES ' + self.reviewed + '; do not APPROVE until fixed'
        self.assertFalse(self.check([rejection])['passed'])
        self.assertFalse(self.check([approval, rejection])['passed'])
        self.assertTrue(self.check([rejection, approval])['passed'])
        rejection['state'] = 'CHANGES_REQUESTED'
        approval['state'] = 'APPROVED'
        self.assertFalse(self.check([approval, rejection])['passed'])
        self.assertTrue(self.check([rejection, approval])['passed'])

    def test_metadata_disambiguates_prior_sha_link(self):
        approval = self.review(self.reviewed)
        approval['commit_id'] = self.reviewed
        approval['body'] += '\nPrior context: ' + self.base
        self.assertTrue(self.check([approval])['passed'])

    def test_string_whitespace_collision_is_rejected(self):
        self.commit('app', 'result = "a b"\n')
        reviewed = self.g('rev-parse', 'HEAD')
        before = content_identity(reviewed, cwd=self.cwd)
        self.commit('app', 'result = "ab"\n')
        after = content_identity(self.g('rev-parse', 'HEAD'), cwd=self.cwd)
        self.assertEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])
        self.assertFalse(self.check([self.review(reviewed)])['passed'])

    def test_python_indentation_collision_is_rejected(self):
        self.commit('app', 'if ready:\n    run()\n')
        reviewed = self.g('rev-parse', 'HEAD')
        before = content_identity(reviewed, cwd=self.cwd)
        self.commit('app', 'if ready:\nrun()\n')
        after = content_identity(self.g('rev-parse', 'HEAD'), cwd=self.cwd)
        self.assertEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])
        self.assertFalse(self.check([self.review(reviewed)])['passed'])

    def test_quoted_non_commit_hex_is_not_a_fetch_target(self):
        # A reviewer quoting a patch-id made the guard fetch it as a commit and
        # abort the whole check (run 34452439985 / 34453959576: "upload-pack:
        # not our ref bfd0296c1f0bdb444ac62e54f95107196440eafb").
        approval = self.review(self.reviewed)
        approval['commit_id'] = self.reviewed
        approval['body'] += '\npatch-id bfd0296c1f0bdb444ac62e54f95107196440eafb'
        head = self.g('rev-parse', 'HEAD')
        self.assertEqual(target_shas([approval], head), {self.reviewed.lower(), head.lower()})
        self.assertTrue(self.check([approval])['passed'])

    def test_explicit_reviewed_sha_resolves_ambiguous_prose(self):
        approval = self.review(self.reviewed)
        approval['body'] = 'APPROVE\nreviewed-sha: ' + self.reviewed + '\nSupersedes ' + self.base
        self.assertTrue(self.check([approval])['passed'])

    def test_delta_source_is_a_fetch_target(self):
        self.commit('app', 'fixed\n')
        head = self.g('rev-parse', 'HEAD')
        approval = self.review(head, self.reviewed)
        self.assertEqual(target_shas([approval], head), {self.reviewed.lower(), head.lower()})


class Pagination(unittest.TestCase):
    def test_concatenated_gh_pages(self):
        self.assertEqual(json_pages('[{"id":1}]\n[{"id":2}]'), [[{'id': 1}], [{'id': 2}]])


if __name__ == '__main__':
    unittest.main()
