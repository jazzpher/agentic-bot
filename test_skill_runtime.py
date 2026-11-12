"""Verified procedural memory lifecycle through the real runtime/tool registry."""
import tempfile
import unittest
from pathlib import Path

from agent_loop import Workspace
from agenticbot import agent as agent_core
from agenticbot.approvals import SandboxPolicy
from agenticbot.tools import ToolContext, default_registry
from test_agenticbot_agent import scripted, text_events, tool_call_events

CANDIDATE = {'name': 'csv-summary', 'description': 'Summarize CSV totals', 'content': '# CSV totals\n1. Read CSV.\n2. Sum values.\n3. Verify against source.'}

class SkillRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.registry = default_registry()
        self.events = []
        self.ctx = ToolContext(workspace=self.ws, session_id='skills', policy=SandboxPolicy(mode='workspace-write'))

    def turn(self, script, verified=True, **kw):
        if verified:
            kw.setdefault('requirements', ['artifact'])
            kw.setdefault('completion_verifier', lambda contract, ctx: {'artifact': {'passed': True, 'evidence': 'Independent artifact check'}})
        return agent_core.run_turn([{'role': 'user', 'content': 'Summarize CSV totals'}], self.registry, self.ctx,
                                  ('key', 'https://api.example.test/v1/chat/completions', 'model-x'),
                                  self.events.append, stream_fn=scripted(script), **kw)

    def test_saves_only_after_acceptance(self):
        def verify(contract, ctx):
            self.assertEqual(self.ws.skills.list(), [])
            return {'artifact': {'passed': True, 'evidence': 'Verified externally'}}
        result = self.turn([tool_call_events('save_skill', CANDIDATE), text_events('Done')], completion_verifier=verify)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(self.ws.skills.read('csv-summary')['name'], 'csv-summary')
        self.assertEqual(len([e for e in self.events if e['type'] == 'skill_saved']), 1)
        self.assertNotIn('_skill_candidates', self.ctx.extras)

    def test_no_verifier_no_save(self):
        self.turn([tool_call_events('save_skill', CANDIDATE), text_events('Done')], verified=False)
        self.assertEqual(self.ws.skills.list(), [])

    def test_failed_verification_discards(self):
        result = self.turn([tool_call_events('save_skill', CANDIDATE)] + [text_events('Done')] * 4,
                           completion_verifier=lambda c, x: {})
        self.assertNotEqual(result.status, 'completed')
        self.assertEqual(self.ws.skills.list(), [])

    def test_truncation_discards(self):
        events = text_events('cut off')
        events[-1]['reason'] = 'length'
        result = self.turn([tool_call_events('save_skill', CANDIDATE), events])
        self.assertNotEqual(result.status, 'completed')
        self.assertEqual(self.ws.skills.list(), [])

    def test_step_limit_discards(self):
        self.turn([tool_call_events('save_skill', CANDIDATE)], max_steps=1)
        self.assertEqual(self.ws.skills.list(), [])

    def test_reuse_next_turn(self):
        self.turn([tool_call_events('save_skill', CANDIDATE), text_events('Done')])
        fn = scripted([tool_call_events('read_skill', {'name': 'csv-summary'}), text_events('Reused CSV steps')])
        result = agent_core.run_turn([{'role': 'user', 'content': 'CSV totals again'}], self.registry, self.ctx,
                                    ('key', 'https://api.example.test/v1/chat/completions', 'model-x'),
                                    self.events.append, stream_fn=fn)
        self.assertEqual(result.status, 'completed')
        self.assertIn('csv-summary', fn.calls[0]['messages'][0]['content'])
        self.assertIn('Sum values', result.messages[2]['content'])

    def test_invalid_candidate_not_staged(self):
        invalid = dict(CANDIDATE, name='../escape')
        self.turn([tool_call_events('save_skill', invalid), text_events('Done')])
        self.assertEqual(self.ws.skills.list(), [])

    def test_oversized_candidate_not_staged(self):
        self.turn([tool_call_events('save_skill', dict(CANDIDATE, content='x' * 13000)), text_events('Done')])
        self.assertEqual(self.ws.skills.list(), [])

    def test_secret_candidate_rejected(self):
        self.turn([tool_call_events('save_skill', dict(CANDIDATE, content='token sk-' + 'a' * 24)), text_events('Done')])
        self.assertEqual(self.ws.skills.list(), [])

    def test_candidate_limit(self):
        script = [tool_call_events('save_skill', dict(CANDIDATE, name='csv-' + str(i)), call_id='c' + str(i)) for i in range(4)]
        self.turn(script + [text_events('Done')])
        self.assertEqual(len(self.ws.skills.list()), 3)

    def test_verified_revision_update(self):
        self.turn([tool_call_events('save_skill', CANDIDATE), text_events('Done')])
        rev = self.ws.skills.read('csv-summary')['revision']
        self.turn([tool_call_events('save_skill', dict(CANDIDATE, content='Improved workflow', revision=rev)), text_events('Done')])
        self.assertNotEqual(self.ws.skills.read('csv-summary')['revision'], rev)

    def test_stale_revision_preserves_skill(self):
        self.turn([tool_call_events('save_skill', CANDIDATE), text_events('Done')])
        rev = self.ws.skills.read('csv-summary')['revision']
        self.turn([tool_call_events('save_skill', dict(CANDIDATE, content='Overwrite', revision='stale')), text_events('Done')])
        self.assertEqual(self.ws.skills.read('csv-summary')['revision'], rev)

    def test_cooperative_stop_discards_and_clears(self):
        def stopped_stream(*args, **kwargs):
            self.ctx.stop.set()
            yield from text_events('Interrupted')
        calls = iter([tool_call_events('save_skill', CANDIDATE)])
        def stream(*args, **kwargs):
            batch = next(calls, None)
            if batch is not None:
                yield from batch
            else:
                yield from stopped_stream()
        result = agent_core.run_turn([{'role': 'user', 'content': 'CSV totals'}], self.registry, self.ctx,
            ('key', 'https://api.example.test/v1/chat/completions', 'model-x'), self.events.append,
            stream_fn=stream, requirements=['artifact'],
            completion_verifier=lambda c, x: {'artifact': {'passed': True, 'evidence': 'Check'}})
        self.assertNotEqual(result.status, 'completed')
        self.assertEqual(self.ws.skills.list(), [])
        self.assertNotIn('_skill_candidates', self.ctx.extras)
        self.assertNotIn('_skill_learning_enabled', self.ctx.extras)

    def test_provider_error_discards_and_clears(self):
        def stream(*args, **kwargs):
            if not self.ctx.extras['_skill_candidates']:
                yield from tool_call_events('save_skill', CANDIDATE)
            else:
                raise RuntimeError('provider unavailable')
        with self.assertRaisesRegex(RuntimeError, 'provider unavailable'):
            agent_core.run_turn([{'role': 'user', 'content': 'CSV totals'}], self.registry, self.ctx,
                ('key', 'https://api.example.test/v1/chat/completions', 'model-x'), self.events.append,
                stream_fn=stream, requirements=['artifact'],
                completion_verifier=lambda c, x: {'artifact': {'passed': True, 'evidence': 'Check'}})
        self.assertEqual(self.ws.skills.list(), [])
        self.assertNotIn('_skill_candidates', self.ctx.extras)
        self.assertNotIn('_skill_learning_enabled', self.ctx.extras)
