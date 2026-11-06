"""Offline regressions for the completion contract, compaction anchor and wire sanitization."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop import Workspace
from grokbot import agent as agent_core, completion, compaction, providers
from grokbot.approvals import SandboxPolicy
from grokbot.tools import ToolContext, default_registry
from test_grokbot_agent import scripted, text_events

REQS = ['archive defaults to false', 'README documents the CLI']


def passing(contract, ctx):
    return {r: {'passed': True, 'evidence': 'independent check ok: ' + r} for r in contract['requirements']}


def failing(contract, ctx):
    ev = passing(contract, ctx)
    ev[REQS[0]] = {'passed': False, 'evidence': 'archive defaulted to true'}
    return ev


class CompletionContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.registry = default_registry()
        self.events = []
        self.cfg = ('key', 'https://api.example.test/v1/chat/completions', 'model-x')

    def ctx(self, **kw):
        return ToolContext(workspace=self.ws, session_id='s1', policy=SandboxPolicy(mode='workspace-write'), **kw)

    def turn(self, stream_fn, message='do it', **kw):
        return agent_core.run_turn([{'role': 'user', 'content': message}], self.registry, self.ctx(), self.cfg,
                                   self.events.append, stream_fn=stream_fn, **kw)

    def checks(self):
        return [e for e in self.events if e['type'] == 'completion_check']

    def test_plain_chat_has_no_gate(self):
        fn = scripted([text_events('hi')])
        result = self.turn(fn, message='hello')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(self.checks(), [])
        self.assertNotIn('Acceptance requirements', fn.calls[0]['messages'][0]['content'])

    def test_requirements_without_verifier_fail_closed_without_retry(self):
        fn = scripted([text_events('done')])
        result = self.turn(fn, requirements=REQS)
        self.assertEqual(result.status, 'incomplete')
        self.assertEqual(len(fn.calls), 1)
        self.assertFalse(self.checks()[0]['passed'])
        self.assertEqual(self.checks()[0]['missing'], REQS)

    def test_passing_verifier_completes_and_prompt_lists_requirements(self):
        fn = scripted([text_events('done')])
        result = self.turn(fn, requirements=REQS, completion_verifier=passing)
        self.assertEqual(result.status, 'completed')
        system = fn.calls[0]['messages'][0]['content']
        for r in REQS:
            self.assertIn(r, system)
        self.assertTrue(self.checks()[0]['passed'])

    def test_failing_verifier_repairs_twice_then_incomplete(self):
        fn = scripted([text_events('done')])
        result = self.turn(fn, requirements=REQS, completion_verifier=failing)
        self.assertEqual(result.status, 'incomplete')
        self.assertEqual(len(fn.calls), 3, 'bounded: two repair attempts')
        repair = [m for m in fn.calls[1]['messages'] if m['role'] == 'user' and 'Independent acceptance failed' in str(m['content'])]
        self.assertEqual(len(repair), 1)
        self.assertIn(REQS[0], repair[0]['content'])
        self.assertNotIn(REQS[1] + '": {', repair[0]['content'], 'only missing evidence is echoed')

    def test_repair_then_pass_completes(self):
        verdicts = iter([failing, passing])
        fn = scripted([text_events('first'), text_events('fixed')])
        result = self.turn(fn, requirements=REQS, completion_verifier=lambda c, x: next(verdicts)(c, x))
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.text, 'fixed')
        self.assertEqual(len(fn.calls), 2)

    def test_malformed_verifiers_fail_closed(self):
        bad = [lambda c, x: ['not', 'a', 'dict'],
               lambda c, x: (_ for _ in ()).throw(RuntimeError('boom')),
               lambda c, x: {r: {'passed': True, 'evidence': '  '} for r in REQS},
               lambda c, x: {r: {'passed': 'yes', 'evidence': 'x'} for r in REQS}]
        for verifier in bad:
            verdict = completion.check({'requirements': REQS}, verifier, None)
            self.assertFalse(verdict['passed'])
            self.assertEqual(verdict['missing'], REQS)

    def test_verifier_cannot_mutate_contract(self):
        def sneaky(contract, ctx):
            contract['requirements'].clear()
            return {}
        contract = {'version': 1, 'request': 'x', 'requirements': list(REQS)}
        self.assertFalse(completion.check(contract, sneaky, None)['passed'])
        self.assertEqual(contract['requirements'], REQS)

    def test_truncated_reply_stays_error_and_skips_verifier(self):
        called = []
        fn = scripted([[{'type': 'text', 'delta': 'partial'}, {'type': 'finish', 'reason': 'length'}]])
        result = self.turn(fn, requirements=REQS, completion_verifier=lambda c, x: called.append(1) or passing(c, x))
        self.assertEqual(result.status, 'error')
        self.assertEqual(called, [])

    def test_new_request_does_not_inherit_old_contract(self):
        fn = scripted([text_events('done')])
        first = self.turn(fn, requirements=REQS, completion_verifier=passing)
        history = first.messages + [{'role': 'user', 'content': 'unrelated: what time is it?'}]
        fn2 = scripted([text_events('noon')])
        result = agent_core_run(self, history, fn2)
        self.assertEqual(result.status, 'completed')
        anchor = result.messages[0][completion.KEY]
        self.assertEqual(anchor['requirements'], [])
        self.assertEqual(anchor['request'], 'unrelated: what time is it?')
        self.assertEqual(sum(1 for m in result.messages if completion.KEY in m), 1)
        self.assertNotIn('Acceptance requirements', fn2.calls[0]['messages'][0]['content'])

    def test_explicit_resume_keeps_contract(self):
        fn = scripted([text_events('done')])
        first = self.turn(fn, message='build it', requirements=REQS)
        self.assertEqual(first.status, 'incomplete')
        history = first.messages + [{'role': 'user', 'content': 'continue'}]
        fn2 = scripted([text_events('done now')])
        result = agent_core_run(self, history, fn2, completion_resume=True, completion_verifier=passing)
        self.assertEqual(result.status, 'completed')
        anchor = result.messages[0][completion.KEY]
        self.assertEqual(anchor['request'], 'build it')
        self.assertEqual(anchor['requirements'], REQS)
        system = fn2.calls[0]['messages'][0]['content']
        self.assertIn('archive defaults to false', system)
        self.assertNotIn('Original request (verbatim', system, 'request still visible verbatim: not duplicated')

    def test_caller_messages_not_mutated(self):
        msgs = [{'role': 'user', 'content': 'go'}]
        snapshot = copy.deepcopy(msgs)
        agent_core_run(self, msgs, scripted([text_events('ok')]), requirements=REQS, completion_verifier=passing)
        self.assertEqual(msgs, snapshot)


def agent_core_run(case, messages, fn, **kw):
    return agent_core.run_turn(messages, case.registry, case.ctx(), case.cfg, case.events.append, stream_fn=fn, **kw)


class CompactionAnchorTests(unittest.TestCase):
    def test_anchor_survives_compaction_verbatim(self):
        msgs = [{'role': 'user', 'content': 'ORIGINAL REQUEST ' + 'x' * 50}]
        contract = completion.attach(msgs, REQS)
        for i in range(30):
            msgs.append({'role': 'assistant', 'content': 'step %d ' % i + 'y' * 400})
            msgs.append({'role': 'user', 'content': 'ok %d' % i})
        out, summary, method = compaction.compact(msgs, None, keep_recent=4)
        anchored = [m for m in out if completion.KEY in m]
        self.assertEqual(len(anchored), 1)
        self.assertEqual(anchored[0][completion.KEY], contract)
        self.assertFalse(any(m.get('content') == msgs[0]['content'] for m in out), 'fixture must really drop the request')
        resumed = completion.attach(out, resume=True)
        self.assertEqual(resumed, contract)
        self.assertIn('ORIGINAL REQUEST', completion.prompt(resumed, out))


class WireMessagesTests(unittest.TestCase):
    def fixture(self):
        return [{'role': 'system', 'content': 's', completion.KEY: {'requirements': REQS}},
                {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function',
                 'function': {'name': 'f', 'arguments': '{}'}, 'extra_content': {'google': {'thought_signature': 'SIG'}}}]},
                {'role': 'user', '_preview_image': True, 'content': [{'type': 'text', 'text': 'shot'},
                 {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}]}]

    def test_strips_private_top_level_keys_only(self):
        msgs = self.fixture()
        before = copy.deepcopy(msgs)
        wire = providers.wire_messages(msgs)
        self.assertEqual(msgs, before, 'local markers survive for scrub/compaction')
        for m in wire:
            self.assertFalse([k for k in m if k.startswith('_')])
        self.assertEqual(wire[1]['tool_calls'][0]['extra_content']['google']['thought_signature'], 'SIG')
        self.assertEqual(wire[2]['content'][1]['image_url']['url'], 'data:image/png;base64,AAAA')

    def _capture(self, call):
        seen = {}

        class Captured(Exception):
            pass

        def fake_post(url, **kw):
            seen.update(kw.get('json') or {})
            raise Captured()
        with patch.object(providers.requests, 'post', side_effect=fake_post):
            with self.assertRaises(Exception):
                call()
        return seen

    def test_stream_chat_payload_is_sanitized(self):
        cfg = ('k', 'https://api.example.test/v1/chat/completions', 'm')
        seen = self._capture(lambda: list(providers.stream_chat(cfg, self.fixture())))
        self.assertIn('messages', seen)
        self.assertNotIn('_preview_image', str(seen['messages']))
        self.assertNotIn(completion.KEY, str(seen['messages']))
        self.assertIn('SIG', str(seen['messages']))

    def test_complete_once_payload_is_sanitized(self):
        cfg = ('k', 'https://api.example.test/v1/chat/completions', 'm')
        seen = self._capture(lambda: providers.complete_once(cfg, self.fixture()))
        self.assertNotIn('_preview_image', str(seen['messages']))
        self.assertNotIn(completion.KEY, str(seen['messages']))


if __name__ == '__main__':
    unittest.main()
