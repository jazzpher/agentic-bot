"""Regression tests for two agent-loop bugs found in an offline review.

1. Tool-call order: in one model step, a call that comes after a write must see that write.
   _execute_calls used to run every read-only call first and the mutating ones after, so
   [write_file(x), read_file(x)] read x before it was written.
2. Truncated replies: a reply cut by the output limit (finish_reason "length"), or a stream that
   ends without [DONE] or a finish_reason ("interrupted"), used to end the turn as "completed".

The legacy loop (agent_loop.run_loop / task_harness) runs one action per model call, so it has no
batch to reorder, and its JSON action parser rejects a cut-off "done" action: covered below.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop import Workspace, run_loop
from agenticbot import agent as agent_core
from agenticbot.approvals import SandboxPolicy
from agenticbot.tools import ToolContext, default_registry


def scripted(script):
    """A fake stream_fn that replays one list of events per model call."""
    count = [0]

    def stream_fn(cfg, messages, tools=None, stop=None, **kwargs):
        step = script[min(count[0], len(script) - 1)]
        count[0] += 1
        for event in step:
            yield event
    return stream_fn


def tool_batch(*calls):
    return [{'type': 'tool_calls', 'calls': [{'id': cid, 'name': name, 'arguments': json.dumps(args)}
                                             for cid, name, args in calls]},
            {'type': 'finish', 'reason': 'tool_calls'}]


def final(text):
    return [{'type': 'text', 'delta': text}, {'type': 'finish', 'reason': 'stop'}]


def sse(*chunks, done=True):
    body = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks)
    return body + ('data: [DONE]\n\n' if done else '')


class FakeSSEResponse:
    """Just enough of requests.Response for providers.stream_chat."""
    status_code = 200
    text = ''

    def __init__(self, body):
        self._body = body.encode('utf-8')

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        yield self._body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LoopTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.registry = default_registry()
        self.events = []
        self.cfg = ('key', 'https://api.example.test/v1/chat/completions', 'model-x')

    def turn(self, stream_fn=None):
        ctx = ToolContext(workspace=self.ws, session_id='s1', policy=SandboxPolicy(mode='workspace-write'))
        return agent_core.run_turn([{'role': 'user', 'content': 'go'}], self.registry, ctx, self.cfg,
                                   self.events.append, stream_fn=stream_fn)

    def tool_output(self, result):
        return {m['tool_call_id']: m['content'] for m in result.messages if m.get('role') == 'tool'}

    def done_status(self):
        return [e for e in self.events if e.get('type') == 'done'][-1]['status']


class ToolOrderTests(LoopTestCase):
    def test_read_after_write_in_same_step_sees_the_write(self):
        fn = scripted([tool_batch(('w', 'write_file', {'path': 'notes.txt', 'content': 'fresh-content-42'}),
                                  ('r', 'read_file', {'path': 'notes.txt'})),
                       final('done')])
        result = self.turn(fn)
        out = self.tool_output(result)
        self.assertIn('fresh-content-42', out['r'], 'read_file ran before the write_file it follows')
        starts = [e['id'] for e in self.events if e.get('type') == 'tool_start']
        self.assertEqual(starts, ['w', 'r'])

    def test_reads_on_each_side_of_a_write_see_old_then_new(self):
        self.ws.path('notes.txt').write_text('old-content-7')
        fn = scripted([tool_batch(('r1', 'read_file', {'path': 'notes.txt'}),
                                  ('w', 'write_file', {'path': 'notes.txt', 'content': 'fresh-content-42'}),
                                  ('r2', 'read_file', {'path': 'notes.txt'})),
                       final('done')])
        result = self.turn(fn)
        out = self.tool_output(result)
        self.assertIn('old-content-7', out['r1'])
        self.assertNotIn('fresh-content-42', out['r1'])
        self.assertIn('fresh-content-42', out['r2'], 'the second read_file ran before the write it follows')
        self.assertEqual(self.ws.path('notes.txt').read_text(), 'fresh-content-42')

    def test_consecutive_reads_still_run_together(self):
        self.ws.path('a.txt').write_text('A')
        self.ws.path('b.txt').write_text('B')
        barrier = threading.Barrier(2, timeout=10)
        real = self.registry.execute

        def execute(ctx, name, args):
            if name == 'read_file':
                barrier.wait()  # both reads must be in flight at once, or this breaks
            return real(ctx, name, args)
        fn = scripted([tool_batch(('1', 'read_file', {'path': 'a.txt'}),
                                  ('2', 'read_file', {'path': 'b.txt'}),
                                  ('3', 'write_file', {'path': 'c.txt', 'content': 'C'})),
                       final('done')])
        with patch.object(self.registry, 'execute', side_effect=execute):
            result = self.turn(fn)
        self.assertFalse(barrier.broken, 'consecutive read-only calls no longer run in parallel')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(self.ws.path('c.txt').read_text(), 'C')


class TruncatedReplyTests(LoopTestCase):
    def assertNotCompleted(self, result):
        self.assertNotEqual(result.status, 'completed', 'a cut-off reply was marked completed')
        self.assertNotEqual(self.done_status(), 'completed')

    def test_reply_cut_by_output_limit_is_not_completed(self):
        fn = scripted([[{'type': 'text', 'delta': 'Steps:\n1. install\n2. con'}, {'type': 'finish', 'reason': 'length'}]])
        result = self.turn(fn)
        self.assertNotCompleted(result)
        self.assertEqual(result.text, 'Steps:\n1. install\n2. con', 'the partial text is kept')

    def test_stream_cut_before_finish_is_not_completed(self):
        fn = scripted([[{'type': 'text', 'delta': 'The answer is'},
                        {'type': 'finish', 'reason': 'interrupted', 'complete': False}]])
        self.assertNotCompleted(self.turn(fn))

    def test_real_stream_hitting_output_limit_is_not_completed(self):
        body = sse({'choices': [{'delta': {'content': 'Steps: 1. inst'}}]},
                   {'choices': [{'delta': {}, 'finish_reason': 'length'}]})
        with patch('agenticbot.providers.requests.post', return_value=FakeSSEResponse(body)):
            result = self.turn()
        self.assertNotCompleted(result)
        self.assertEqual(result.text, 'Steps: 1. inst')

    def test_real_stream_dropped_before_done_is_not_completed(self):
        body = sse({'choices': [{'delta': {'content': 'The answer is'}}]}, done=False)
        with patch('agenticbot.providers.requests.post', return_value=FakeSSEResponse(body)):
            result = self.turn()
        self.assertNotCompleted(result)

    def test_real_stream_finished_normally_completes(self):
        body = sse({'choices': [{'delta': {'content': 'All good.'}}]},
                   {'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
        with patch('agenticbot.providers.requests.post', return_value=FakeSSEResponse(body)):
            result = self.turn()
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.text, 'All good.')


class LegacyLoopTests(unittest.TestCase):
    def test_cut_off_done_action_is_never_accepted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = Workspace(Path(tmp.name) / 'ws')
        events = list(run_loop('summarise the notes', [], ws, lambda messages: '{"action": "done", "text": "All fin',
                               lambda: False))
        types = [e['type'] for e in events]
        self.assertNotIn('done', types)
        self.assertEqual(types[-1], 'blocked')


if __name__ == '__main__':
    unittest.main()
