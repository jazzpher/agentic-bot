"""Weak-model robustness: malformed tool arguments are repaired, wrong tool names get a suggestion."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_loop import Workspace
from agenticbot import agent as agent_core
from agenticbot.approvals import SandboxPolicy
from agenticbot.tools import ToolContext, default_registry, repair_arguments, suggest_tool


class RepairArgumentsTests(unittest.TestCase):
    def test_valid_json_is_untouched(self):
        self.assertEqual(repair_arguments('{"path": "a.txt", "n": 2}'), {'path': 'a.txt', 'n': 2})

    def test_trailing_comma(self):
        self.assertEqual(repair_arguments('{"path": "a.txt",}'), {'path': 'a.txt'})

    def test_python_literals(self):
        self.assertEqual(repair_arguments('{"overwrite": True, "note": None}'), {'overwrite': True, 'note': None})

    def test_single_quoted_dict(self):
        self.assertEqual(repair_arguments("{'path': 'a.txt'}"), {'path': 'a.txt'})

    def test_fenced_block(self):
        self.assertEqual(repair_arguments('```json\n{"path": "a.txt"}\n```'), {'path': 'a.txt'})

    def test_prose_around_the_object(self):
        self.assertEqual(repair_arguments('sure, here it is: {"path": "a.txt"} hope that helps'), {'path': 'a.txt'})

    def test_double_encoded(self):
        self.assertEqual(repair_arguments(json.dumps(json.dumps({'path': 'a.txt'}))), {'path': 'a.txt'})

    def test_empty_is_empty_dict(self):
        self.assertEqual(repair_arguments('   '), {})

    def test_hopeless_returns_none(self):
        self.assertIsNone(repair_arguments('I will read the file now'))


class SuggestToolTests(unittest.TestCase):
    NAMES = ['run_command', 'list_files', 'read_file', 'write_file', 'web_search']

    def test_alias(self):
        self.assertEqual(suggest_tool('bash', self.NAMES), ['run_command'])
        self.assertEqual(suggest_tool('ls', self.NAMES), ['list_files'])

    def test_case_insensitive_exact(self):
        self.assertEqual(suggest_tool('Read_File', self.NAMES), ['read_file'])

    def test_close_match(self):
        self.assertIn('read_file', suggest_tool('readfile', self.NAMES))

    def test_nothing_close(self):
        self.assertEqual(suggest_tool('teleport', self.NAMES), [])


class RegistryExecuteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.registry = default_registry()
        self.ctx = ToolContext(workspace=self.ws, session_id='s1', policy=SandboxPolicy(mode='workspace-write'))

    def test_malformed_arguments_still_run_the_tool(self):
        result = self.registry.execute(self.ctx, 'write_file', '{"path": "a.txt", "content": "hi",}')
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self.ws.path('a.txt').read_text(), 'hi')

    def test_unrepairable_arguments_say_what_to_send(self):
        result = self.registry.execute(self.ctx, 'write_file', 'write a file please')
        self.assertFalse(result.ok)
        self.assertIn('JSON object', result.output)

    def test_unknown_tool_suggests_the_right_name(self):
        result = self.registry.execute(self.ctx, 'bash', {'command': 'echo hi'})
        self.assertFalse(result.ok)
        self.assertIn('Did you mean run_command', result.output)


def scripted(script):
    calls = []

    def stream_fn(cfg, messages, tools=None, stop=None, **kwargs):
        calls.append({'messages': [dict(m) for m in messages]})
        step = script[min(len(calls) - 1, len(script) - 1)]
        for event in step:
            yield event
    stream_fn.calls = calls
    return stream_fn


def text_events(text):
    return [{'type': 'text', 'delta': text}, {'type': 'usage', 'prompt_tokens': 10, 'completion_tokens': 2},
            {'type': 'finish', 'reason': 'stop'}]


class TurnWithMalformedCallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.registry = default_registry()
        self.events = []
        self.cfg = ('key', 'https://api.example.test/v1/chat/completions', 'model-x')

    def ctx(self):
        return ToolContext(workspace=self.ws, session_id='s1', policy=SandboxPolicy(mode='workspace-write'))

    def turn(self, fn, **kw):
        return agent_core.run_turn([{'role': 'user', 'content': 'write it'}], self.registry, self.ctx(), self.cfg,
                                   self.events.append, stream_fn=fn, **kw)

    def test_native_call_with_trailing_comma_runs(self):
        fn = scripted([[{'type': 'tool_calls', 'calls': [{'id': 'c1', 'name': 'write_file',
                                                          'arguments': '{"path": "a.txt", "content": "hi",}'}]},
                        {'type': 'finish', 'reason': 'tool_calls'}], text_events('Done.')])
        result = self.turn(fn)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(self.ws.path('a.txt').read_text(), 'hi')
        ends = [e for e in self.events if e['type'] == 'tool_end']
        self.assertTrue(ends and ends[0]['ok'], ends)

    def test_json_protocol_call_with_python_literals_runs(self):
        fn = scripted([[{'type': 'text', 'delta': '{"tool": "write_file", "arguments": {"path": "b.txt", "content": "yo", "overwrite": True}}'},
                        {'type': 'finish', 'reason': 'stop'}], text_events('Done.')])
        result = self.turn(fn, tool_mode='json')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(self.ws.path('b.txt').read_text(), 'yo')

    def test_wrong_tool_name_is_told_the_right_one(self):
        fn = scripted([[{'type': 'tool_calls', 'calls': [{'id': 'c1', 'name': 'bash',
                                                          'arguments': '{"command": "echo hi"}'}]},
                        {'type': 'finish', 'reason': 'tool_calls'}], text_events('Could not.')])
        self.turn(fn)
        ends = [e for e in self.events if e['type'] == 'tool_end']
        self.assertTrue(ends and not ends[0]['ok'])
        self.assertIn('Did you mean run_command', ends[0]['output'])


if __name__ == '__main__':
    unittest.main()
