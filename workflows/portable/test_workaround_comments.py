"""Workaround-comment gate: an added unlinked marker fails, everything else passes."""
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from workaround_comments import added_lines, comment_text, line_finding, lint, main


def quiet_main(argv):
    """Exit code of `main`, with its report swallowed so test output stays readable."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return main(argv)


class CommentDetection(unittest.TestCase):
    def test_unlinked_marker_is_a_finding(self):
        for path, line, marker in (
            ('app/main.py', '    # HACK: retry loop papers over the cold sweep', 'HACK'),
            ('web/app.js', '  // workaround: fallback when the backend 503s', 'WORKAROUND'),
            ('db/views.sql', '-- FIXME: index build cancelled by the pooler', 'FIXME'),
            ('web/index.html', '<!-- TODO: rebuild this table -->', 'TODO'),
        ):
            with self.subTest(line=line):
                finding = line_finding(path, 7, line)
                self.assertIsNotNone(finding)
                self.assertEqual((finding['line'], finding['marker']), (7, marker))

    def test_comment_linking_an_issue_is_accepted(self):
        for path, line in (
            ('a.py', '# TODO: fold into the coordinator (https://github.com/Wladefant/super-board/issues/245)'),
            ('a.py', '# HACK: needs the wallet helper, see Wladefant/super-board#245'),
            ('a.py', '# FIXME: migrate after #245 lands'),
            ('a.js', '// WORKAROUND: pinned until issue #4106 closes'),
        ):
            with self.subTest(line=line):
                self.assertIsNone(line_finding(path, 3, line))

    def test_code_and_prose_are_not_comments(self):
        for path, line in (
            ('a.py', 'todo_queue.append(item)'),
            ('a.py', 'MARKER_WORDS = ("HACK", "WORKAROUND")'),
            ('a.py', '"""TODO: document the retry policy."""'),
            ('a.py', 'description = "# TODO: fix later"'),
            ('a.js', 'log("// TODO: fix later")'),
            ('a.yml', 'todo_list: [one, two]'),
            ('docs/plan.md', '# TODO: rewrite the deployment section'),
            ('docs/plan.md', '* HACK: keep the old heading'),
            ('data/notes.json', '  "note": "// TODO: fix later",'),
            ('data/notes.txt', '# HACK: keep the retry loop'),
        ):
            with self.subTest(path=path, line=line):
                self.assertIsNone(line_finding(path, 1, line))

    def test_marker_must_be_a_whole_word(self):
        for line in ('# TODOS are tracked in the ledger', '# TODO_ITEMS is a tuple',
                     '# hackathon notes', '# FIXMEs are listed in the runbook'):
            with self.subTest(line=line):
                self.assertIsNone(line_finding('a.py', 1, line))

    def test_trailing_comment_is_a_finding(self):
        finding = line_finding('backend/app/x.py', 12, 'retries = 3  # HACK: third retry hides the timeout')
        self.assertIsNotNone(finding)
        self.assertEqual(finding['comment'], '# HACK: third retry hides the timeout')

    def test_comment_without_a_marker_is_ignored(self):
        self.assertIsNone(line_finding('a.py', 1, '# The retry loop is bounded by the sweep timeout'))

    def test_comment_text_returns_the_trailing_comment(self):
        self.assertEqual(comment_text('.js', 'const a = 1;  // TODO: name it'),
                         '// TODO: name it')
        self.assertIsNone(comment_text('.md', '# TODO: prose, not code'))


class UnifiedDiffScanning(unittest.TestCase):
    def test_added_lines_follow_the_hunk_header(self):
        diff = (
            'diff --git a/app/main.py b/app/main.py\n'
            'index 1111111..2222222 100644\n'
            '--- a/app/main.py\n'
            '+++ b/app/main.py\n'
            '@@ -10,0 +11,2 @@ def handler():\n'
            '+value = 1\n'
            '+other = 2\n'
        )
        self.assertEqual([(path, number) for path, number, _ in added_lines(diff)],
                         [('app/main.py', 11), ('app/main.py', 12)])

    def test_removed_lines_are_ignored(self):
        diff = (
            '--- a/app/main.py\n'
            '+++ b/app/main.py\n'
            '@@ -4,2 +4,1 @@\n'
            '-# HACK: drop this once the retry lands\n'
            '+# the retry now lands here\n'
        )
        self.assertEqual(lint(diff), [])

    def test_linked_added_comment_passes(self):
        diff = (
            '--- a/app/main.py\n'
            '+++ b/app/main.py\n'
            '@@ -0,0 +1,2 @@\n'
            '+# TODO: fold into the coordinator (https://github.com/Wladefant/super-board/issues/245)\n'
            '+value = 1\n'
        )
        self.assertEqual(lint(diff), [])

    def test_unlinked_added_comment_reports_path_and_line(self):
        diff = (
            '--- a/app/main.py\n'
            '+++ b/app/main.py\n'
            '@@ -0,0 +1,2 @@\n'
            '+"""Receiver."""\n'
            '+# WORKAROUND: poll instead of subscribing\n'
        )
        self.assertEqual(lint(diff), [{
            'path': 'app/main.py',
            'line': 2,
            'marker': 'WORKAROUND',
            'comment': '# WORKAROUND: poll instead of subscribing',
        }])

    def test_context_lines_advance_the_new_line_number(self):
        diff = (
            '--- a/app/main.py\n'
            '+++ b/app/main.py\n'
            '@@ -5,3 +5,4 @@\n'
            ' import os\n'
            '+# TODO: revisit the import below\n'
            ' from . import other\n'
            ' value = 2\n'
        )
        self.assertEqual([item['line'] for item in lint(diff)], [6])

    def test_deleted_file_adds_nothing(self):
        diff = (
            '--- a/app/old.py\n'
            '+++ /dev/null\n'
            '@@ -1,2 +0,0 @@\n'
            '-# HACK: remove me\n'
            '-value = 1\n'
        )
        self.assertEqual(lint(diff), [])

    def test_unknown_file_types_are_never_scanned(self):
        diff = (
            '--- a/docs/plan.md\n'
            '+++ b/docs/plan.md\n'
            '@@ -0,0 +1,2 @@\n'
            '+# TODO: rewrite this section\n'
            '+# HACK: keep the old heading\n'
            '--- a/data/notes.json\n'
            '+++ b/data/notes.json\n'
            '@@ -0,0 +1,1 @@\n'
            '+{"note": "TODO: fix later"}\n'
        )
        self.assertEqual(lint(diff), [])


class CommandLine(unittest.TestCase):
    """The gate reads a real checkout, so these run against a throwaway repository."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        self._git('init')
        self.commit('app/main.py', 'value = 1\n', 'base')

    def _git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def commit(self, relative, text, message):
        target = self.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        self._git('add', '-A')
        self._git('-c', 'user.name=Comment Lint', '-c', 'user.email=lint@example.invalid',
                  'commit', '-m', message)

    def test_unlinked_marker_exits_one(self):
        self.commit('app/main.py', 'value = 1\n# HACK: pinned until the sweep is batched\n', 'unlinked')
        self.assertEqual(quiet_main(['--base', 'HEAD~1', '--repo-root', str(self.repo)]), 1)

    def test_linked_marker_exits_zero(self):
        self.commit('app/main.py', 'value = 1\n# HACK: pinned, see #245\n', 'linked')
        self.assertEqual(quiet_main(['--base', 'HEAD~1', '--repo-root', str(self.repo)]), 0)

    def test_unresolvable_base_exits_two(self):
        self.commit('app/main.py', 'value = 2\n', 'clean change')
        self.assertEqual(quiet_main(['--base', 'origin/does-not-exist', '--repo-root', str(self.repo)]), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
