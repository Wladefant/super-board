"""Exercise the portable merge gate using the canonical real-git scenarios."""
import os
import unittest
import test_review_content as fixtures
import github_pr_gate


class InstalledGateReviews(fixtures.ContentReviews):
    def check(self, reviews, staging=True):
        previous = os.getcwd()
        try:
            os.chdir(self.cwd)
            result = github_pr_gate.evaluate_pr_gate({
                'number': 1, 'state': 'OPEN', 'headRefOid': self.g('rev-parse', 'HEAD'),
                'baseRefName': 'staging', 'baseRefOid': self.base,
                'author': {'login': 'author'}, 'reviews': reviews,
                'statusCheckRollup': [],
            }, repo='Bavariance/polysimulator' if staging else 'example/fixture')
            self.last_result = result
            return {'passed': result.gate_verdict == 'PASSED'}
        finally:
            os.chdir(previous)

    def test_author_comment_is_not_formal_approval(self):
        self.assertTrue(self.check([self.review(self.reviewed, actor='author')])['passed'])
        self.assertEqual(self.last_result.approval_verdict, 'AUTOMATED_REVIEW_APPROVED')


if __name__ == '__main__':
    unittest.main(defaultTest='InstalledGateReviews')
