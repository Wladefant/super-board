#!/usr/bin/env python3
"""test_ste_check.py - each STE rule fires on a bad input and stays quiet on the fixed input."""

import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ste_check as ste


def rules(text, **kw):
    return sorted({f["rule"] for f in ste.check_text(text, **kw)["findings"]})


class Rules(unittest.TestCase):
    def test_long_sentence_is_an_error_and_split_passes(self):
        long = " ".join(["word"] * 26) + "."
        r = ste.check_text(long)
        self.assertEqual(r["errors"], 1)
        self.assertEqual(rules(" ".join(["word"] * 12) + ". " + " ".join(["Word"] * 12) + "."), [])

    def test_twenty_one_words_is_only_a_warning(self):
        r = ste.check_text(" ".join(["word"] * 21) + ".")
        self.assertEqual((r["errors"], r["warnings"]), (0, 1))

    def test_semicolon(self):
        self.assertIn("semicolon", rules("The job failed; the log is empty."))
        self.assertNotIn("semicolon", rules("The job failed. The log is empty."))

    def test_passive_with_actor_and_hidden_actor(self):
        self.assertIn("passive-voice", rules("The PR was rejected by the gate."))
        self.assertIn("passive-voice", rules("The file was written to disk."))
        self.assertNotIn("passive-voice", rules("The gate rejects the PR."))

    def test_state_participle_is_not_passive(self):
        self.assertNotIn("passive-voice", rules("The PR is merged."))
        self.assertNotIn("passive-voice", rules("The check is blocked."))

    def test_plain_word_replacements(self):
        r = ste.check_text("We utilize the cache prior to the run.")
        fixes = {f["fix"] for f in r["findings"] if f["rule"] == "plain-word"}
        self.assertEqual(fixes, {"Use 'use'.", "Use 'before'."})
        self.assertEqual(rules("We use the cache before the run."), [])

    def test_phrasal_verb_and_idiom(self):
        self.assertIn("plain-word", rules("Spin up the server."))
        self.assertIn("idiom", rules("This is the smoking gun."))

    def test_paragraph_longer_than_six_sentences(self):
        para = " ".join(f"Step {i} is short." for i in range(7))
        self.assertIn("paragraph-length", rules(para))
        self.assertNotIn("paragraph-length", rules(para.rsplit(" Step", 1)[0]))

    def test_state_first_only_for_texts_of_four_sentences_or_more(self):
        body = "We read the log. We found a file. We changed it. We ran it."
        self.assertIn("state-first", rules(body))
        self.assertNotIn("state-first", rules("Done. " + body))
        self.assertNotIn("state-first", rules("We read the log."))

    def test_mixed_terminology_is_info_and_not_scored(self):
        r = ste.check_text("The lane stopped. The worker restarted.")
        self.assertEqual([f["severity"] for f in r["findings"] if f["rule"] == "terminology"], ["info"])
        self.assertEqual(r["score"], 100.0)

    def test_contraction_only_in_strict_mode(self):
        self.assertEqual(rules("It doesn't work."), [])
        self.assertIn("contraction", rules("It doesn't work.", strict=True))


class Markup(unittest.TestCase):
    def test_code_links_and_fences_are_ignored(self):
        text = (
            "Done. Run `a; b; c` now.\n```\n" + " ".join(["x"] * 40) + ";\n```\n"
            "See [the log](https://example.com/a/very/long/path?x=1) for more.\n"
        )
        self.assertEqual(ste.check_text(text)["errors"], 0)

    def test_machine_lines_and_tables_are_skipped(self):
        text = "QA-RECEIPT: PASS " + "a" * 40 + "; extra; words\n| a; b | c |\n"
        r = ste.check_text(text)
        self.assertEqual(r["sentences"], 0)

    def test_telegram_html_is_stripped_before_checking(self):
        text = "<b>Done.</b> Use <code>a; b</code> and <a href=\"https://x.test\">this link</a>."
        r = ste.check_text(text)
        self.assertEqual((r["errors"], r["warnings"]), (0, 0))

    def test_list_items_are_separate_sentences(self):
        text = "Steps:\n1. Stop the job\n2. Start the job\n"
        self.assertEqual(ste.check_text(text)["sentences"], 3)

    def test_a_long_list_is_not_one_long_paragraph(self):
        items = "\n".join(f"- Item {i} is short." for i in range(9))
        self.assertNotIn("paragraph-length", rules(items))

    def test_abbreviation_and_version_do_not_split_sentences(self):
        self.assertEqual(ste.check_text("Use v5.5 e.g. the sonnet lane.")["sentences"], 1)


class Scoring(unittest.TestCase):
    def test_score_drops_with_violations(self):
        bad = ste.check_text("The cache was utilized prior to the run; it failed.")
        good = ste.check_text("The run used the cache. It failed.")
        self.assertLess(bad["score"], good["score"])
        self.assertEqual(good["score"], 100.0)

    def test_empty_text_scores_100(self):
        self.assertEqual(ste.check_text("")["score"], 100.0)

    def test_skill_before_after_example(self):
        before = ("I wasn't able to determine yet why Main died again; it went down at 15:46 UTC "
                  "with nothing in the logs, and this has now happened 13 times within the last week, "
                  "always being terminated externally by Windows before our code could log anything.")
        after = ("Main stopped again at 15:46 UTC. I do not know the cause yet. The logs show no error.")
        self.assertLess(ste.check_text(before)["score"], 50)
        self.assertEqual(ste.check_text(after)["errors"], 0)


class Cli(unittest.TestCase):
    def run_main(self, argv, text):
        import io
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin = type("S", (), {"buffer": io.BytesIO(text.encode())})()
        sys.stdout = io.StringIO()
        try:
            rc = ste.main(argv)
            return rc, sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out

    def test_default_never_blocks_but_fail_under_does(self):
        text = "The cache was utilized prior to the run; it failed."
        self.assertEqual(self.run_main([], text)[0], 0)
        self.assertEqual(self.run_main(["--fail-under", "90"], text)[0], 1)

    def test_explicit_check_with_stdin_dash_after_options(self):
        rc, out = self.run_main(["check", "--format", "html", "--summary-json", "-"], "<b>Done.</b> Use it.")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["sentences"], 2)

    def test_metrics_line_is_appended(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m", "ste.jsonl")
            self.run_main(["--metrics", p, "--source", "unit"], "Done. Use the cache.")
            with open(p, encoding="utf-8") as fh:
                rec = json.loads(fh.read().strip())
            self.assertEqual((rec["source"], rec["score"]), ("unit", 100.0))

    def test_rewrite_prompt_carries_the_post_prompt_and_findings(self):
        rc, out = self.run_main(["rewrite-prompt"], "We utilize it.")
        self.assertIn("Use ASD-STE100 as a guide", out)
        self.assertIn("Preserve technical precision and uncertainty.", out)
        self.assertIn("plain-word", out)


if __name__ == "__main__":
    unittest.main()
