"""Durable checkpoint callback boundaries and fail-closed persistence."""
import copy
import unittest
from unittest.mock import patch

from agenticbot import compaction
from test_agenticbot_agent import RunTurnTests, scripted, text_events, tool_call_events


class CheckpointTests(RunTurnTests):
    # Reuse setup, not inherited test cases (see load_tests below).
    def test_checkpoints_native_and_json_are_detached(self):
        for mode in ('native', 'json'):
            with self.subTest(mode=mode):
                saved = []
                events = tool_call_events('write_file', {'path': mode + '.txt', 'content': 'synthetic'})
                if mode == 'json':
                    events = text_events('{"tool":"write_file","arguments":{"path":"json.txt","content":"synthetic"}}')
                result = self.turn(scripted([events, text_events('Finished writing.')]),
                                   tool_mode=mode, checkpoint=lambda m, s: saved.append((m, s)),
                                   previous_summary='earlier summary')
                self.assertEqual(len(saved), 4)  # start, intent, completed batch, final
                self.assertIn('outcome unknown', str(saved[1][0]))
                self.assertNotIn('outcome unknown', str(saved[2][0]))
                self.assertIn('UNTRUSTED TOOL RESULT', str(saved[2][0]))
                self.assertEqual(saved[0][1], 'earlier summary')
                result.messages[0]['content'] = 'changed after return'
                self.assertNotEqual(saved[0][0][0]['content'], 'changed after return')

    def test_failed_intent_save_prevents_tool_execution(self):
        saves = []
        def checkpoint(messages, summary):
            saves.append(messages)
            if len(saves) == 2:
                raise OSError('disk full')
        with self.assertRaisesRegex(OSError, 'disk full'):
            self.turn(scripted([tool_call_events('write_file', {'path': 'no.txt', 'content': 'x'})]),
                      checkpoint=checkpoint)
        self.assertFalse(self.ws.path('no.txt').exists())
        self.assertFalse(any(e['type'] == 'tool_start' for e in self.events))

    def test_failed_post_save_keeps_unknown_intent_and_stops(self):
        saves = []
        def checkpoint(messages, summary):
            if len(saves) == 2:
                raise OSError('disk full after tool')
            saves.append(copy.deepcopy(messages))
        stream = scripted([tool_call_events('write_file', {'path': 'once.txt', 'content': 'x'}), text_events('x')])
        with self.assertRaisesRegex(OSError, 'disk full after tool'):
            self.turn(stream, checkpoint=checkpoint)
        self.assertEqual(len(stream.calls), 1)
        self.assertEqual(self.ws.path('once.txt').read_text(), 'x')
        self.assertIn('outcome unknown', str(saves[-1]))

    def test_compaction_summary_is_checkpointed(self):
        saved = []
        compacted = [{'role': 'user', 'content': 'original request'}]
        with patch.object(compaction, 'needs_compaction', return_value=True), \
             patch.object(compaction, 'compact', return_value=(compacted, 'new summary', 'model')):
            self.turn(scripted([text_events('Hello.')]), checkpoint=lambda m, s: saved.append((m, s)))
        self.assertEqual(saved[1], ([{'role': 'user', 'content': 'original request'}], 'new summary'))

    def test_preview_scrub_does_not_mutate_live_history(self):
        saved = []
        messages = [{'role': 'user', '_preview_image': True, 'content': [
            {'type': 'text', 'text': 'screen'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,eA=='}}]}]
        from agenticbot import agent
        def stream(cfg, history, **kw):
            self.assertTrue(history[1]['_preview_image'])
            yield from text_events('Hello.')
        agent.run_turn(messages, self.registry, self.ctx(), self.cfg, self.events.append,
                       stream_fn=stream, checkpoint=lambda m, s: saved.append(m))
        self.assertNotIn('_preview_image', saved[0][0])
        self.assertIn('screenshot omitted', saved[0][0]['content'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(CheckpointTests(name) for name in CheckpointTests.__dict__ if name.startswith('test_'))
