import subprocess
import sys
import json
import contextlib
import io
import unittest
from unittest.mock import patch

import review_content
from github_pr_gate import _content_binder


class GitDeadlineTests(unittest.TestCase):
    def test_git_has_deadline_and_closed_stdin(self):
        with patch('review_content.subprocess.check_output', return_value=b'ok') as call:
            self.assertEqual(review_content.git('cat-file', '-e', 'a' * 40), 'ok')
        self.assertEqual(call.call_args.kwargs['timeout'], 10)
        self.assertEqual(call.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_ancestor_has_deadline_and_closed_stdin(self):
        with patch('review_content.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as call:
            self.assertTrue(review_content.is_ancestor('a' * 40, 'b' * 40))
        self.assertEqual(call.call_args.kwargs['timeout'], 10)
        self.assertEqual(call.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_unresponsive_git_blocks_even_exact_head_receipt(self):
        with patch('review_content.subprocess.check_output', side_effect=subprocess.TimeoutExpired('git', 10)):
            binds, _, error = _content_binder('a' * 40, 'main', None)
        self.assertFalse(binds('a' * 40))
        self.assertIn('timed out', error)

    def test_patch_stdin_is_explicit_and_bounded(self):
        with patch('review_content.subprocess.check_output', return_value=b'ok') as call:
            review_content.git('patch-id', '--stable', input=b'diff')
        self.assertEqual(call.call_args.kwargs['input'], b'diff')
        self.assertEqual(call.call_args.kwargs['timeout'], 10)

    def test_real_unresponsive_child_expires(self):
        real_check_output = subprocess.check_output
        def stalled_git(args, **kwargs):
            return real_check_output([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        with patch('review_content.subprocess.check_output', side_effect=stalled_git):
            binds, _, error = _content_binder('a' * 40, 'main', None)
        self.assertFalse(binds('a' * 40))
        self.assertIn('timed out', error)

    def test_receipt_fetch_git_calls_are_bounded(self):
        from github_pr_gate import fetch_pr_json
        responses = [json.dumps({'baseRefName': 'main', 'headRefOid': 'a' * 40}), 'b' * 40, '[]', '[]']
        with patch('github_pr_gate._run_gh', side_effect=[subprocess.CompletedProcess([], 0, text, '') for text in responses]), \
             patch('github_pr_gate.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
            fetch_pr_json(1, 'owner/repo')
        self.assertGreater(len(run.call_args_list), 0)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['timeout'], 10)
            self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)
            if call.args[0][1] == 'fetch':
                self.assertIn('--depth=200', call.args[0])

    def test_cli_timeout_reports_blocked(self):
        import github_pr_gate
        errors = io.StringIO()
        with patch('sys.argv', ['github_pr_gate.py', '--pr', '1', '--repo', 'owner/repo']), \
             patch('github_pr_gate.fetch_pr_json', side_effect=subprocess.TimeoutExpired('git', 10)), \
             contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as exit_result:
            github_pr_gate.main()
        self.assertNotEqual(exit_result.exception.code, 0)
        self.assertIn('BLOCKED', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
