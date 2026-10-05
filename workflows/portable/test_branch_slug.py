import unittest

from branch_slug import branch_name


class BranchName(unittest.TestCase):
    def test_folds_unicode_and_punctuation(self):
        self.assertEqual(branch_name(7, "Fix: \u00dcn\u00efcode  bug!"), "issue-7-fix-unicode-bug")

    def test_truncates_at_word_boundary_within_limit(self):
        name = branch_name(12, "word " * 30)
        self.assertLessEqual(len(name), 60)
        self.assertFalse(name.endswith("-"))

    def test_collision_gets_numeric_suffix(self):
        taken = frozenset({"issue-7-a-b"})
        self.assertEqual(branch_name(7, "a b", taken), "issue-7-a-b-2")
        self.assertEqual(branch_name(7, "a b", taken | {"issue-7-a-b-2"}), "issue-7-a-b-3")

    def test_empty_title_falls_back(self):
        self.assertEqual(branch_name(3, "!!!"), "issue-3-work")


if __name__ == "__main__":
    unittest.main()
