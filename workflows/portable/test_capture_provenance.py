import hashlib
from pathlib import Path
import github_pr_gate
import json
import unittest
from unittest.mock import patch

from github_pr_gate import shot_provenance_problems, evaluate_qa_receipt, evaluate_flow_qa_receipt

BEFORE = 'b' * 40
AFTER = 'a' * 40


class CaptureProvenanceTests(unittest.TestCase):
    def body(self, mutation=None, missing=False):
        records = []
        captions = []
        for label, sha, digest in [('before', BEFORE, '1' * 64), ('after', AFTER, '2' * 64)]:
            record = {'label': label, 'served_sha': sha, 'account': 'qa-user', 'viewport': '390x844',
                      'device_scale': 1, 'url': 'http://127.0.0.1:4799/app', 'sha256': digest, 'source': 'application'}
            if label == 'after' and mutation:
                record.update(mutation)
            records.append('CAPTURE ' + json.dumps(record))
            captions.append(f'SHOT {label} served={sha} expected={sha} viewport=390x844 sha256={digest} phash=1234567890123456')
        return ('![before](before.png) ![after](after.png)\n' + '\n'.join(captions) +
                '\nSHOT-PAIR viewport=390x844 phash_dist=12 changed_ratio=0.2\n' +
                ('' if missing else '\n'.join(records)))

    def check(self, **kwargs):
        return shot_provenance_problems(self.body(**kwargs), lambda sha: sha == AFTER, True)

    def test_polysimulator_keeps_its_existing_caption_check(self):
        body = ('QA-RECEIPT: PASS ' + AFTER + '\n' + self.body(missing=True)
                .replace('before.png', 'https://github.com/user-attachments/assets/00000001-1111-2222-3333-000000000001')
                .replace('after.png', 'https://github.com/user-attachments/assets/00000002-1111-2222-3333-000000000002'))
        data = {'files': [{'path': 'frontend/components/Ticket.tsx'}], 'comments': [{'body': body}]}
        with patch('github_pr_gate._content_binder', return_value=(lambda sha: sha == AFTER, [AFTER], None)):
            result = evaluate_qa_receipt(data, repo='Bavariance/polysimulator', base_ref='staging', head_sha=AFTER)
        self.assertEqual(result[0], 'PASSED', result[1])

    def test_shipnovo_requires_capture_records_through_project_config(self):
        root = Path(github_pr_gate.__file__).parent
        runner = hashlib.sha256((root / 'flow_qa_runner.mjs').read_bytes()).hexdigest()
        flow = hashlib.sha256((root / 'flows/shipnovo.json').read_bytes()).hexdigest()
        body = (f'FLOW-QA: PASS {AFTER}\nFLOW-QA-ASSERTIONS pass=7 fail=0\n'
                'FLOW-QA-VIEWPORTS 390x844,390x420,1440x900\n'
                f'FLOW-QA-SOURCE runner={runner} flow={flow} project=shipnovo\n'
                + self.body(missing=True))
        data = {'files': [{'path': 'src/app/page.tsx'}], 'comments': [{'body': body}]}
        with patch('github_pr_gate._content_binder', return_value=(lambda sha: sha == AFTER, [AFTER], None)):
            result = evaluate_flow_qa_receipt(data, repo='Wladefant/shipnovo', base_ref='main', head_sha=AFTER)
        self.assertEqual(result[0], 'REQUIRED', result[1])

    def test_shipnovo_rejects_separate_qa_comment_missing_capture_records(self):
        root = Path(github_pr_gate.__file__).parent
        runner = hashlib.sha256((root / 'flow_qa_runner.mjs').read_bytes()).hexdigest()
        flow = hashlib.sha256((root / 'flows/shipnovo.json').read_bytes()).hexdigest()
        flow_body = (f'FLOW-QA: PASS {AFTER}\nFLOW-QA-ASSERTIONS pass=7 fail=0\n'
                     'FLOW-QA-VIEWPORTS 390x844,390x420,1440x900\n'
                     f'FLOW-QA-SOURCE runner={runner} flow={flow} project=shipnovo\n')
        qa_body = self.body(missing=True)
        data = {'files': [{'path': 'src/app/page.tsx'}], 'comments': [{'body': flow_body}, {'body': qa_body}]}
        with patch('github_pr_gate._content_binder', return_value=(lambda sha: sha == AFTER, [AFTER], None)):
            result = evaluate_flow_qa_receipt(data, repo='Wladefant/shipnovo', base_ref='main', head_sha=AFTER)
        self.assertEqual(result[0], 'REQUIRED', result[1])
        self.assertIn('capture provenance failed', result[1])
    def test_shipnovo_accepts_separate_qa_comment_with_valid_capture_records(self):
        root = Path(github_pr_gate.__file__).parent
        runner = hashlib.sha256((root / 'flow_qa_runner.mjs').read_bytes()).hexdigest()
        flow = hashlib.sha256((root / 'flows/shipnovo.json').read_bytes()).hexdigest()
        flow_body = (f'FLOW-QA: PASS {AFTER}\nFLOW-QA-ASSERTIONS pass=7 fail=0\n'
                     'FLOW-QA-VIEWPORTS 390x844,390x420,1440x900\n'
                     f'FLOW-QA-SOURCE runner={runner} flow={flow} project=shipnovo\n')
        qa_body = self.body(missing=False)
        data = {'files': [{'path': 'src/app/page.tsx'}], 'comments': [{'body': flow_body}, {'body': qa_body}]}
        with patch('github_pr_gate._content_binder', return_value=(lambda sha: sha == AFTER, [AFTER], None)):
            result = evaluate_flow_qa_receipt(data, repo='Wladefant/shipnovo', base_ref='main', head_sha=AFTER)
            qa_result = evaluate_qa_receipt(data, repo='Wladefant/shipnovo', base_ref='main', head_sha=AFTER)
        self.assertEqual(result[0], 'PASSED', result[1])
        self.assertEqual(qa_result[0], 'PASSED', qa_result[1])


    def test_earlier_head_capture_does_not_block_current_receipt(self):
        root = Path(github_pr_gate.__file__).parent
        runner = hashlib.sha256((root / 'flow_qa_runner.mjs').read_bytes()).hexdigest()
        flow = hashlib.sha256((root / 'flows/shipnovo.json').read_bytes()).hexdigest()
        body = (f'FLOW-QA: PASS {AFTER}\nFLOW-QA-ASSERTIONS pass=7 fail=0\n'
                'FLOW-QA-VIEWPORTS 390x844,390x420,1440x900\n'
                f'FLOW-QA-SOURCE runner={runner} flow={flow} project=shipnovo\n')
        old = self.body().replace(AFTER, 'c' * 40)
        data = {'files': [{'path': 'src/app/page.tsx'}], 'comments': [
            {'body': old}, {'body': body}, {'body': self.body()}]}
        with patch('github_pr_gate._content_binder', return_value=(lambda sha: sha == AFTER, [AFTER], None)):
            flow_result = evaluate_flow_qa_receipt(data, repo='Wladefant/shipnovo', base_ref='main', head_sha=AFTER)
            qa_result = evaluate_qa_receipt(data, repo='Wladefant/shipnovo', base_ref='main', head_sha=AFTER)
        self.assertEqual(flow_result[0], 'PASSED', flow_result[1])
        self.assertEqual(qa_result[0], 'PASSED', qa_result[1])

    def test_valid_measured_records(self):
        self.assertEqual(self.check(), [])

    def test_missing_capture_records(self):
        self.assertTrue(self.check(missing=True))

    def test_forged_head_caption(self):
        self.assertTrue(self.check(mutation={'served_sha': BEFORE}))

    def test_account_mismatch(self):
        self.assertTrue(self.check(mutation={'account': 'other-user'}))

    def test_mixed_viewports(self):
        self.assertTrue(self.check(mutation={'viewport': '1440x900'}))

    def test_mixed_device_scale(self):
        self.assertTrue(self.check(mutation={'device_scale': 2}))

    def test_static_mockup(self):
        self.assertTrue(self.check(mutation={'source': 'static-mockup', 'url': 'file:///mockup.html'}))

    def test_missing_account(self):
        self.assertTrue(self.check(mutation={'account': ''}))

    def test_image_hash_mismatch(self):
        self.assertTrue(self.check(mutation={'sha256': '3' * 64}))

    def test_later_viewport_cannot_hide_forged_capture(self):
        body = self.body() + "\n" + self.body(mutation={'account': ''}).replace('390x844', '1440x900')
        self.assertTrue(shot_provenance_problems(body, lambda sha: sha == AFTER, True))


if __name__ == '__main__':
    unittest.main()
