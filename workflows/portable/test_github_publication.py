"""Two publication/recovery regressions; every transport is isolated."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import github_plan_renderer as renderer
import github_work_item as work
from test_github_work_item import api_issue


class GitHubTransport:
    def __init__(self):
        self.issue = api_issue()
        self.comments = []
        self.hide_after_write = False

    def __call__(self, query, variables):
        if 'addComment' in query:
            comment = {
                'id': 'COMMENT', 'url': self.issue['url'] + '#issuecomment-1',
                'body': variables['body'], 'author': {'login': 'operator'},
                'createdAt': '2026-09-13T12:00:00Z', 'updatedAt': '2026-09-13T12:00:00Z',
            }
            self.comments.append(comment)
            return {'data': {'addComment': {'commentEdge': {'node': comment}}}}
        if 'repository(' in query:
            issue = copy.deepcopy(self.issue)
            issue['comments'] = {
                'nodes': [] if self.hide_after_write else copy.deepcopy(self.comments),
                'pageInfo': {'hasNextPage': False, 'endCursor': None},
            }
            return {'data': {'repository': {'issue': issue}}}
        # A direct-node read can succeed while ordinary recovery still loses it.
        return {'data': {'node': copy.deepcopy(self.comments[-1])}}

    def subprocess_run(self, cmd, **options):
        variables = dict(value.split('=', 1) for value in cmd[4::2])
        query = variables.pop('query')
        payload = json.dumps(self(query, variables), ensure_ascii=False).encode('utf-8')
        # Reproduce gh UTF-8 output on a Windows host whose default is cp1252.
        return subprocess.CompletedProcess(cmd, 0, payload.decode(options.get('encoding') or 'cp1252'), '')


class PublicationRegressions(unittest.TestCase):
    def test_write_must_be_readable_by_normal_recovery(self):
        api = GitHubTransport()
        record = {'github': {'issue_url': api.issue['url']}}
        text = 'Operator correction — retain the original failure and these acceptance steps.'
        with patch('project_adapter.subprocess.run', side_effect=api.subprocess_run):
            url = work.publish_report(api.issue['url'], text)
            view = work.execution_view(record, work.fetch_work_item(record))
        self.assertIn(text, view['prompt'])
        self.assertIn(url, view['prompt'])
        api = GitHubTransport()
        api.hide_after_write = True
        with self.assertRaises(ValueError):
            work.publish_report(api.issue['url'], text, api)

    def test_empty_transport_raises_instead_of_printing_success(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'evidence.txt'
            report.write_text('Evidence', encoding='utf-8')
            argv = ['renderer', 'post-issue-comment', '--issue', '114', '--repo',
                    'Wladefant/super-board', '--file', str(report)]
            output = io.StringIO()
            with patch.object(sys, 'argv', argv), patch.object(
                renderer, 'gh_cli_run', return_value=(0, '', ''), create=True
            ), patch('project_adapter.default_graphql_runner', return_value={}), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    renderer.main()
            self.assertNotEqual(raised.exception.code, 0)
            self.assertNotIn('Successfully', output.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
