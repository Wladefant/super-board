import unittest
import json
from unittest.mock import patch, MagicMock
import test_telegram_notifier as transport_fixtures
from telegram_notifier import NotificationEvent, TelegramNotificationAdapter, inline_text, truncate_html, format_consolidated_blockers_presentation

URL = 'https://github.com/Wladefant/super-board/pull/84'

class MediaContracts(unittest.TestCase):
    setUp = transport_fixtures.TestTelegramNotificationAdapter.setUp
    tearDown = transport_fixtures.TestTelegramNotificationAdapter.tearDown

    def test_photo_album_and_choice_payloads(self):
        for count in (1, 2):
            event = NotificationEvent('question' if count == 1 else 'status', 'polysimulator', 'sample', 'Use this image?', URL,
                                      {'images': ['https://example.com/image.png'] * count, 'options': [{'id': 'A', 'label': 'Yes'}]},
                                      session_id='session-test')
            result = {'message_id': 321, 'from': {'id': 123}, 'chat': {'id': 456}}
            response = MagicMock()
            response.read.side_effect = [
                json.dumps({'ok': True, 'result': result if count == 1 else [result, dict(result, message_id=322)]}).encode(),
                json.dumps({'ok': True, 'result': dict(result, message_id=323)}).encode(),
            ]
            with patch('urllib.request.urlopen') as send:
                send.return_value.__enter__.return_value = response
                receipt = self.adapter.notify(event, force=True)
                request = send.call_args_list[0].args[0]
                self.assertTrue(receipt.delivered)
                payload = json.loads(request.data)
                self.assertTrue(request.full_url.endswith('/sendPhoto' if count == 1 else '/sendMediaGroup'))
                if count == 1:
                    self.assertEqual(payload['parse_mode'], 'HTML')
                    self.assertEqual(payload['caption'], TelegramNotificationAdapter.format_message(event))
                    self.assertTrue(payload['reply_markup']['inline_keyboard'][0][0]['callback_data'].startswith('cb:d_'))
                    self.assertEqual(payload['reply_markup']['inline_keyboard'][-1], [{'text': 'Open on GitHub', 'url': URL}])
                else:
                    self.assertEqual(payload['media'][0]['caption'], TelegramNotificationAdapter.format_message(event))
                    self.assertEqual(payload['media'][0]['parse_mode'], 'HTML')
                    self.assertNotIn('caption', payload['media'][1])
                    controls = json.loads(send.call_args_list[1].args[0].data)
                    self.assertEqual(controls['text'], 'Actions for the images above')
                    self.assertEqual(controls['reply_markup']['inline_keyboard'][-1], [{'text': 'Open on GitHub', 'url': URL}])
                    for message_id in (321, 322, 323):
                        self.assertEqual(self.adapter.correlation_store.lookup('123', '456', message_id)['session_id'], 'session-test')


class CardContracts(unittest.TestCase):
    def event(self, kind='status', **metadata):
        return NotificationEvent(kind, 'Wladefant/super-board', 'card', 'Ready for review\nNothing deployed', URL, metadata)

    def test_exact_rich_card_and_optional_link(self):
        event = NotificationEvent('status', 'Cards', 'rich', 'Now: Ready & checked\nNext: Await feedback', '', {'long_detail': 'Optional <context>'})
        self.assertEqual(TelegramNotificationAdapter.format_message(event), '📊 <b>Status update</b>\nCards\n\n• <b>Now:</b> Ready &amp; checked\n• <b>Next:</b> Await feedback\n\n<blockquote expandable>Optional &lt;context&gt;</blockquote>')

    def test_exact_standard_templates(self):
        for kind, title in [('status', '📊 <b>Status update</b>'), ('milestone', '🚀 <b>Milestone reached</b>'), ('blocker', '🛑 <b>Blocked</b>'), ('completion', '✅ <b>Completed</b>')]:
            with self.subTest(kind=kind):
                self.assertEqual(TelegramNotificationAdapter.format_message(self.event(kind)), title + '\n<a href="' + URL + '">Wladefant/super-board</a>\n\n• Ready for review\n• Nothing deployed')

    def test_exact_question_decision_reminder(self):
        for kind, reminder, title in [('question', False, '❓ <b>Question</b>'), ('decision', False, '❓ <b>Decision needed</b>'), ('decision', True, '🔔 <b>Decision reminder</b>')]:
            event = self.event(kind, problem='Spacing is tight.', proposed_action='Use compact cards.', consequence_or_risk='No behavior changes.', question='Keep this style?', options=[{'id': 'A', 'label': 'Yes'}, {'id': 'B', 'label': 'No'}], is_due_reminder=reminder)
            self.assertEqual(TelegramNotificationAdapter.format_message(event), title + '\n<a href="' + URL + '">Wladefant/super-board</a>\n\n• Spacing is tight.\n• <b>Proposal:</b> Use compact cards.\n• <b>Impact:</b> No behavior changes.\n\n<b>Keep this style?</b>\nA = Yes\nB = No')

    def test_exact_consolidation(self):
        self.assertEqual(format_consolidated_blockers_presentation([{'topic': 'Card spacing', 'canonical_link': URL, 'problem': 'Choose density.', 'proposed_action': 'Keep compact.', 'consequence_or_risk': 'Visual only.'}]), '🔔 <b>Decisions waiting</b>\n\n<b><a href="' + URL + '">Card spacing</a></b>\nChoose density.\n<b>Proposal:</b> Keep compact.\n<b>Impact:</b> Visual only.\n\n<b>Which decision should we address first?</b>\nReply with the topic name.')

    def test_exact_photo_caption(self):
        self.assertEqual(TelegramNotificationAdapter.format_message(self.event(screenshot='https://example.com/image.png')), '📊 <b>Status update</b>\n<a href="' + URL + '">Wladefant/super-board</a>\n\n• Ready for review\n• Nothing deployed')

    def test_short_body_preserves_overflow_in_expandable_detail(self):
        event = self.event()
        event.summary = "First\nSecond\nThird\nFourth\n" + "Long " * 40
        self.assertEqual(TelegramNotificationAdapter.format_message(event), '📊 <b>Status update</b>\n<a href="' + URL + '">Wladefant/super-board</a>\n\n• First\n• Second\n• Third\n\n<blockquote expandable>Fourth\n' + ("Long " * 40).strip() + '</blockquote>')

    def test_escaping_and_existing_anchor(self):
        self.assertEqual(inline_text('<script> & "x" <a href="' + URL + '">PR #84 &amp; UI</a>', 'Wladefant/super-board'), '&lt;script&gt; &amp; &quot;x&quot; <a href="' + URL + '">PR #84 &amp; UI</a>')
        self.assertEqual(inline_text('[PR #84](' + URL + ')'), '<a href="' + URL + '">PR #84</a>')
        self.assertEqual(inline_text('<a href="javascript:bad">x</a>'), 'x')

    def test_sha_and_mentions(self):
        sha = 'a' * 40
        self.assertEqual(inline_text('PR #84 at ' + sha, 'Wladefant/super-board'), 'PR <a href="' + URL + '">#84</a> at <a href="https://github.com/Wladefant/super-board/commit/' + sha + '"><code>aaaaaaaa</code></a>')

    def test_exact_truncation(self):
        self.assertEqual(truncate_html('<b>abcdefghijk</b>', 12), '<b>abcd…</b>')
        self.assertEqual(truncate_html('<b>&amp;&amp;&amp;</b>', 13), '<b>&amp;…</b>')
        for metadata, limit in [({}, 4096), ({'images': ['https://example.com/a.png']}, 1024)]:
            event = self.event(**metadata, long_detail='🙂 & ' * 3000)
            rendered = TelegramNotificationAdapter.format_message(event)
            self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, limit)
            self.assertTrue(rendered.endswith('</blockquote>'))

if __name__ == '__main__':
    unittest.main()
