"""Offline regressions: text-only models refusing screenshot parts, and short provider rate limits."""
import copy
import tempfile
import unittest
from pathlib import Path

import requests

from agent_loop import Workspace
from grokbot import agent as agent_core
from grokbot.approvals import SandboxPolicy
from grokbot.tools import ToolContext, default_registry
from test_grokbot_agent import text_events

URL = 'https://api.groq.com/openai/v1/chat/completions'
SHOT = {'role': 'user', '_preview_image': True, 'content': [
    {'type': 'text', 'text': 'Screenshot of the page browse_open returned.'},
    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA', 'detail': 'auto'}}]}
TEXT_ONLY_400 = '{"error":{"message":"messages[1].content must be a string","type":"invalid_request_error"}}'


def http_error(code, body='', headers=None):
    response = requests.Response()
    response.status_code = code
    response._content = body.encode()
    response.url = URL
    response.headers.update(headers or {})
    return requests.HTTPError('%d Client Error' % code, response=response)


class Plan:
    """A stream_fn that raises or streams one scripted step per call and records what it was sent."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, cfg, messages, tools=None, stop=None, **_):
        self.calls.append(copy.deepcopy(messages))
        step = self.steps.pop(0)

        def gen():
            if isinstance(step, Exception):
                raise step
            yield from step
        return gen()


def image_parts(messages):
    return [p for m in messages if isinstance(m.get('content'), list)
            for p in m['content'] if isinstance(p, dict) and p.get('type') == 'image_url']


class TextOnlyAndRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.events = []
        self.ctx = ToolContext(workspace=self.ws, session_id='s1', policy=SandboxPolicy(mode='workspace-write'))

    def turn(self, plan, messages=None):
        messages = messages if messages is not None else [{'role': 'user', 'content': 'hi'}]
        return agent_core.run_turn(messages, default_registry(), self.ctx, ('key', URL, 'openai/gpt-oss-20b'),
                                   self.events.append, stream_fn=plan)

    def kinds(self, kind):
        return [e for e in self.events if e['type'] == kind]

    def test_text_only_model_drops_screenshots_and_keeps_text(self):
        attached = {'role': 'user', 'content': [{'type': 'text', 'text': 'compare with this mockup'},
                                                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,BBBB'}}]}
        original = [{'role': 'user', 'content': 'open the page'}, dict(SHOT), attached]
        plan = Plan(http_error(400, TEXT_ONLY_400), text_events('Example Domain'))
        result = self.turn(plan, original)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(plan.calls), 2)
        self.assertEqual(len(image_parts(plan.calls[0])), 2)
        self.assertEqual(image_parts(plan.calls[1]), [])
        sent = [str(m.get('content')) for m in plan.calls[1]]
        self.assertFalse(any('Screenshot of the page' in c for c in sent), 'preview-only message dropped')
        noted = [m for m in plan.calls[1] if 'compare with this mockup' in str(m.get('content'))]
        self.assertEqual(len(noted), 1)
        self.assertNotIn('earlier context was compacted', plan.calls[1][0]['content'], 'flattened request still counts as visible')
        self.assertIsInstance(noted[0]['content'], str)
        self.assertIn('accepts text only', noted[0]['content'])
        self.assertTrue(self.ctx.extras.get('images_unsupported'))
        self.assertIsInstance(original[1]['content'], list, 'caller history is not mutated')
        self.assertIsInstance(original[2]['content'], list, 'caller history is not mutated')
        self.assertEqual(self.kinds('error'), [])

    def test_image_rejection_is_retried_only_once(self):
        plan = Plan(http_error(400, TEXT_ONLY_400), http_error(400, TEXT_ONLY_400))
        result = self.turn(plan, [dict(SHOT), {'role': 'user', 'content': 'x'}])
        self.assertEqual(result.status, 'error')
        self.assertEqual(len(plan.calls), 2)

    def test_same_400_without_images_is_not_retried(self):
        plan = Plan(http_error(400, TEXT_ONLY_400))
        result = self.turn(plan)
        self.assertEqual(result.status, 'error')
        self.assertEqual(len(plan.calls), 1)
        self.assertFalse(self.ctx.extras.get('images_unsupported'))

    def test_unrelated_400_with_images_still_errors(self):
        plan = Plan(http_error(400, '{"error":{"message":"The model does not exist"}}'))
        result = self.turn(plan, [dict(SHOT), {'role': 'user', 'content': 'x'}])
        self.assertEqual(result.status, 'error')
        self.assertEqual(len(plan.calls), 1)
        self.assertFalse(self.ctx.extras.get('images_unsupported'))

    def test_short_retry_after_waits_and_retries(self):
        plan = Plan(http_error(429, '{}', {'retry-after': '0.01'}), text_events('ok'))
        result = self.turn(plan)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(plan.calls), 2)
        self.assertEqual(len(self.kinds('retry')), 1)
        self.assertIn('Rate limit', self.kinds('retry')[0]['text'])

    def test_reset_tokens_header_is_used(self):
        plan = Plan(http_error(429, '{}', {'x-ratelimit-reset-tokens': '20ms'}), text_events('ok'))
        result = self.turn(plan)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(plan.calls), 2)

    def test_long_or_unknown_rate_limit_fails_fast(self):
        for headers in ({'retry-after': '3600'}, {}, {'retry-after': 'Wed, 21 Oct 2026 07:28:00 GMT'},
                        {'x-ratelimit-reset-tokens': '50ms', 'x-ratelimit-remaining-requests': '0'}):
            with self.subTest(headers=headers):
                self.events = []
                plan = Plan(http_error(429, '{}', headers))
                result = self.turn(plan)
                self.assertEqual(result.status, 'error')
                self.assertEqual(len(plan.calls), 1)
                self.assertIn('Free quota reached', self.kinds('error')[-1]['text'])

    def test_rate_limit_retries_are_bounded(self):
        n = agent_core.MAX_RETRIES + 1
        plan = Plan(*[http_error(429, '{}', {'retry-after': '0.01'}) for _ in range(n)])
        result = self.turn(plan)
        self.assertEqual(result.status, 'error')
        self.assertEqual(len(plan.calls), n)
        self.assertEqual(len(self.kinds('retry')), agent_core.MAX_RETRIES)

    def test_parse_wait_units(self):
        p = agent_core._parse_wait
        self.assertAlmostEqual(p('585ms'), 0.585)
        self.assertAlmostEqual(p('7.66s'), 7.66)
        self.assertAlmostEqual(p('4m19.2s'), 259.2)
        self.assertAlmostEqual(p('2'), 2.0)
        self.assertAlmostEqual(p('1h'), 3600.0)
        for bad in ('', None, 'soon'):
            self.assertIsNone(p(bad))


if __name__ == '__main__':
    unittest.main()

