import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from github_pr_gate import evaluate_flow_qa_receipt

ROOT = Path(__file__).parent
HEAD = 'a' * 40


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GuardIntegrityTests(unittest.TestCase):
    def receipt(self, runner=None, flow=None):
        runner = runner or digest(ROOT / 'flow_qa_runner.mjs')
        flow = flow or digest(ROOT / 'flows/shipnovo.json')
        return (f'FLOW-QA: PASS {HEAD}\nFLOW-QA-ASSERTIONS pass=7 fail=0\n'
                'FLOW-QA-VIEWPORTS 390x844,390x420,1440x900\n'
                f'FLOW-QA-SOURCE runner={runner} flow={flow} project=shipnovo\n')

    def check_receipt(self, body):
        data = {'files': [{'path': 'src/app/page.tsx'}], 'comments': [{'body': body}]}
        with patch('github_pr_gate._content_binder', return_value=(lambda sha: sha == HEAD, [HEAD], None)):
            return evaluate_flow_qa_receipt(data, repo='Wladefant/shipnovo', base_ref='main', head_sha=HEAD)[0]

    def test_canonical_guards_pass(self):
        self.assertEqual(self.check_receipt(self.receipt()), 'PASSED')

    def test_reduced_runner_threshold_is_rejected(self):
        original = (ROOT / 'flow_qa_runner.mjs').read_bytes()
        altered = original.replace(b'>= 44', b'>= 1')
        self.assertNotEqual(original, altered)
        self.assertEqual(self.check_receipt(self.receipt(runner=hashlib.sha256(altered).hexdigest())), 'REQUIRED')

    def test_removed_flow_check_is_rejected(self):
        original = (ROOT / 'flows/shipnovo.json').read_bytes()
        changed = original.replace(b'"tap_target_min_44"', b'"visible"')
        self.assertNotEqual(original, changed)
        self.assertEqual(self.check_receipt(self.receipt(flow=hashlib.sha256(changed).hexdigest())), 'REQUIRED')

    def test_same_content_different_served_sha_is_rejected(self):
        data = {'files': [{'path': 'src/app/page.tsx'}],
                'comments': [{'body': self.receipt().replace('FLOW-QA: PASS ' + HEAD, 'FLOW-QA: PASS ' + 'b' * 40)}]}
        with patch('github_pr_gate._content_binder', return_value=(lambda sha: True, [HEAD], None)):
            result = evaluate_flow_qa_receipt(data, repo='Wladefant/shipnovo', base_ref='main', head_sha=HEAD)
        self.assertEqual(result[0], 'REQUIRED')

    def test_later_pass_cannot_erase_a_failed_target_on_unchanged_content(self):
        data = {'files': [{'path': 'src/app/page.tsx'}], 'comments': [
            {'body': f'FLOW-QA: FAIL {HEAD}', 'created_at': '2026-10-09T10:00:00Z'},
            {'body': self.receipt(), 'created_at': '2026-10-09T11:00:00Z'},
        ]}
        with patch('github_pr_gate._content_binder', return_value=(lambda sha: sha == HEAD, [HEAD], None)):
            result = evaluate_flow_qa_receipt(data, repo='Wladefant/shipnovo', base_ref='main', head_sha=HEAD)
        self.assertEqual(result[0], 'REQUIRED')
        self.assertIn('failed target', result[1])

    def test_missing_source_is_rejected(self):
        self.assertEqual(self.check_receipt(self.receipt().split('FLOW-QA-SOURCE')[0]), 'REQUIRED')


if __name__ == '__main__':
    unittest.main()
