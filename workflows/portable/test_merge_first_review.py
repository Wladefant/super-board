"""Review controls: real CI, guarded version fetch, and identity-safe recovery.

A merging lane owns recovery. Unknown or mismatched served identity requires Main,
not a revert. PolySimulator only permits staging, even with a safe hostname.
"""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_pr_gate as gate
import post_deploy_qa as deploy

SHA = 'a' * 40

class ReviewControls(unittest.TestCase):
    def test_qa_only_needs_real_ci_or_local_record(self):
        for conclusion in ('SUCCESS', 'FAILURE', 'PENDING'):
            with self.subTest(conclusion=conclusion):
                pr = {'number': 1, 'state': 'OPEN', 'headRefOid': SHA,
                      'baseRefName': 'main', 'author': {'login': 'Wladefant'},
                      'files': [{'path': 'site/page.tsx', 'additions': 1, 'deletions': 0}],
                      'statusCheckRollup': [{'name': 'superboard/exact-sha-qa',
                                             'conclusion': conclusion, 'status': 'COMPLETED'}]}
                result = gate.evaluate_pr_gate(pr, repo='Wladefant/super-board', env={'SUPERBOARD_MERGE_FIRST': '1'})
                self.assertNotEqual(result.gate_verdict, 'PASSED')
                record = {'head_sha': SHA, 'passed': 2, 'failed': 0, 'commands': ['tsc --noEmit', 'vitest run page.test.ts']}
                result = gate.evaluate_pr_gate(pr, repo='Wladefant/super-board', env={'SUPERBOARD_MERGE_FIRST': '1'}, local_tests_record=record)
                self.assertEqual(result.gate_verdict, 'PASSED')

    @patch.object(deploy, 'validate_target_url')
    @patch.object(deploy, 'fetch_served_sha', return_value=SHA)
    @patch.object(deploy, 'run_flow_qa', return_value={'passed': True, 'receipt': f'FLOW-QA: PASS {SHA}'})
    @patch.object(deploy, 'post_github_comment', return_value=(0, '', ''))
    @patch.object(deploy, 'update_project_card_status', return_value=MagicMock(ok=True))
    def test_poly_main_refused_before_network(self, card, post, flow, fetch, validate):
        with self.assertRaises(ValueError):
            deploy.run_post_deploy_qa('https://staging.example', SHA, 'main', 1, 2, 'deploy staging', repo='Bavariance/polysimulator')
        validate.assert_not_called()
        fetch.assert_not_called()

    @patch.object(deploy, 'validate_target_url')
    @patch.object(deploy, 'fetch_served_sha', return_value='b' * 40)
    @patch.object(deploy, 'update_project_card_status', return_value=MagicMock(ok=True))
    @patch.object(deploy, 'post_github_comment', return_value=(0, '', ''))
    def test_mismatch_never_authorizes_revert(self, post, card, fetch, validate):
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            result = deploy.run_post_deploy_qa('https://test.dev', SHA, 'main', 1, 2, 'deploy', repo='Wladefant/pinthread')
        self.assertFalse(result['ok'])
        self.assertIn('served SHA does not match: do not revert, ask Main', out.getvalue())
        self.assertNotIn('git revert', out.getvalue())

    @patch.object(deploy.urllib.request, 'urlopen')
    @patch.object(deploy.urllib.request, 'build_opener')
    @patch.object(deploy, 'validate_target_url', return_value='https://test.dev/api/version')
    def test_actual_version_request_checks_final_host(self, validate, build, urlopen):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = 'https://polysimulator.com/api/version'
        response.read.return_value = json.dumps({'sha': SHA}).encode()
        urlopen.return_value = response
        build.return_value.open.return_value = response
        with self.assertRaises(ValueError):
            deploy.fetch_served_sha('https://test.dev/api/version')
        urlopen.assert_not_called()

    @patch.object(deploy.subprocess, 'run')
    def test_recovery_binds_merge_commit_not_pr_head(self, run):
        run.return_value = MagicMock(returncode=0, stdout=json.dumps({'state': 'MERGED', 'baseRefName': 'main', 'mergeCommit': {'oid': SHA}}))
        self.assertTrue(deploy.verify_merge_identity('Wladefant/pinthread', 1, SHA, 'main'))
        self.assertFalse(deploy.verify_merge_identity('Wladefant/pinthread', 1, 'b' * 40, 'main'))
        self.assertFalse(deploy.verify_merge_identity('Wladefant/pinthread', 1, SHA, 'staging'))

if __name__ == '__main__':
    unittest.main()
