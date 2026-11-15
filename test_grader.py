"""Independent grader: synthetic end-to-end checks (no real records, no network)."""
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from agent_loop import Workspace
from agenticbot import agent, grader
from agenticbot.approvals import SandboxPolicy
from agenticbot.tools import ToolContext, default_registry
from test_agenticbot_agent import scripted, text_events, tool_call_events

CFG = ('key', 'https://api.example.test/v1/chat/completions', 'test')
UNSORTED = 'Surname,Given\nReyes,Ana\nÁlvarez,Ben\nCruz,Lea\n'
SORTED = 'Surname,Given\nÁlvarez,Ben\nCruz,Lea\nReyes,Ana\n'


class FakeGrader:
    """Stands in for the grader's own model call; records exactly what it was shown."""

    def __init__(self, criteria, judge=None, fail=False):
        self.criteria, self.judge, self.fail, self.seen = criteria, judge or {}, fail, []

    def __call__(self, messages):
        self.seen.append(messages)
        if self.fail:
            raise RuntimeError('provider down')
        if messages[0]['content'] == grader.CRITERIA_PROMPT:
            return 'sure: ' + json.dumps({'criteria': self.criteria})
        return json.dumps({'results': [{'id': k, 'passed': v, 'reason': 'from file'} for k, v in self.judge.items()]})


class GraderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.ctx = ToolContext(workspace=Workspace(self.root / 'ws'), session_id='grader',
                               policy=SandboxPolicy(mode='workspace-write'))
        env = patch.dict(os.environ, {'AGENTIC_GRADER_DIR': str(self.root / 'runs'), 'AGENTIC_GRADER': 'on',
                                      'AGENTIC_GRADER_ROUNDS': '3'})
        env.start()
        self.addCleanup(env.stop)
        self.events = []

    def turn(self, request, script, grader_fn):
        stream = scripted(script)
        result = agent.run_turn([{'role': 'user', 'content': request}], default_registry(), self.ctx, CFG,
                                self.events.append, stream_fn=stream, verify_request=True, grader_fn=grader_fn)
        return result, stream

    def checks(self):
        return [e for e in self.events if e.get('type') == 'completion_check']

    def write(self, path, content, cid='c1'):
        return tool_call_events('write_file', {'path': path, 'content': content}, cid)

    SORT_CRITERIA = [{'text': 'people.csv Surname column is in A-Z order',
                      'check': {'kind': 'sort', 'path': 'people.csv', 'column': 'Surname'}},
                     {'text': 'people.csv exists', 'check': {'kind': 'file', 'path': 'people.csv'}}]

    def test_nvidia_nemotron_grader_disables_thinking_for_json_budget(self):
        cfg = ('key', 'https://integrate.api.nvidia.com/v1/chat/completions',
               'nvidia/nemotron-3.5-super-vl-120b-a12b')
        messages = [{'role': 'system', 'content': grader.CRITERIA_PROMPT}]
        with patch('agenticbot.providers.complete_once', return_value='{"criteria": []}') as call:
            self.assertEqual(grader.model_grader(cfg)(messages), '{"criteria": []}')
        call.assert_called_once_with(cfg, messages, max_tokens=1500,
                                     extra={'chat_template_kwargs': {'enable_thinking': False}})

    def test_other_providers_do_not_receive_nvidia_template_options(self):
        for url, model in [('https://api.example.test/v1/chat/completions', 'nemotron'),
                           ('https://integrate.api.nvidia.com/v1/chat/completions', 'other-model'),
                           ('https://integrate.api.nvidia.com.evil.test/v1/chat/completions', 'nemotron')]:
            cfg = ('key', url, model)
            with self.subTest(url=url, model=model), patch('agenticbot.providers.complete_once') as call:
                grader.model_grader(cfg, max_tokens=2000)([])
                call.assert_called_once_with(cfg, [], max_tokens=2000, extra=None)

    def test_false_done_is_caught_then_fixed_with_only_failed_items_sent_back(self):
        fake = FakeGrader(self.SORT_CRITERIA)
        result, stream = self.turn('Sort people.csv by Surname', [
            self.write('people.csv', UNSORTED), text_events('Finished the file.'),
            self.write('people.csv', SORTED, 'c2'), text_events('Finished the file.')], fake)
        self.assertEqual(result.status, 'completed')
        first, last = self.checks()[0], self.checks()[-1]
        self.assertFalse(first['passed'])
        self.assertIn('Not A-Z', first['checks'][0]['evidence'])
        self.assertTrue(last['passed'])
        self.assertIn('measured', last['checks'][0]['evidence'])
        note = [m for m in stream.calls[2]['messages'] if m.get('role') == 'user'][-1]['content']
        self.assertIn('GRADER', note)
        self.assertIn('Surname column', note)
        self.assertNotIn('people.csv exists', note)          # passed criteria are not sent back
        criteria_calls = [m for m in fake.seen if m[0]['content'] == grader.CRITERIA_PROMPT]
        self.assertEqual(len(criteria_calls), 1)            # criteria fixed after the first grade
        self.assertNotIn('Finished the file', json.dumps(fake.seen))   # grader never sees the builder's words
        rounds = sorted(p.name for p in pathlib.Path(last['versions']).parent.iterdir())
        self.assertEqual(rounds, ['round-1', 'round-2'])     # every version kept
        kept = pathlib.Path(first['versions'], 'files', 'people.csv').read_text(encoding='utf-8')
        self.assertEqual(kept, UNSORTED)

    def test_three_rounds_max_then_incomplete(self):
        result, stream = self.turn('Sort people.csv by Surname', [
            self.write('people.csv', UNSORTED), text_events('Finished.')], FakeGrader(self.SORT_CRITERIA))
        self.assertEqual(result.status, 'incomplete')
        self.assertEqual([c['round'] for c in self.checks()], [1, 2, 3])
        self.assertNotEqual(result.status, 'completed')

    def test_builder_editing_tests_never_passes(self):
        ws = self.ctx.workspace.root
        (ws / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        (ws / 'test_calc.py').write_text('import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n'
                                         '    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n')
        cheat = ('import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n'
                 '    def test_add(self):\n        self.assertEqual(add(2, 3), -1)\n')
        crit = [{'text': 'unit tests pass', 'check': {'kind': 'tests', 'command': 'python3 -m unittest test_calc'}}]
        result, _ = self.turn('Fix add() in calc.py so the tests pass', [
            self.write('test_calc.py', cheat), text_events('Fixed.')], FakeGrader(crit))
        self.assertNotEqual(result.status, 'completed')
        self.assertIn('test file(s) were changed', self.checks()[0]['checks'][0]['evidence'])

    def test_grader_runs_tests_itself(self):
        ws = self.ctx.workspace.root
        (ws / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        (ws / 'test_calc.py').write_text('import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n'
                                         '    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n')
        crit = [{'text': 'unit tests pass', 'check': {'kind': 'tests', 'command': 'python3 -m unittest test_calc'}}]
        result, _ = self.turn('Fix add() in calc.py so the tests pass', [
            self.write('calc.py', 'def add(a, b):\n    return a - b  # still wrong\n'), text_events('Fixed.'),
            self.write('calc.py', 'def add(a, b):\n    return a + b\n', 'c2'), text_events('Fixed.')], FakeGrader(crit))
        self.assertEqual(result.status, 'completed', self.checks())
        self.assertFalse(self.checks()[0]['passed'])
        self.assertIn('Grader ran', self.checks()[-1]['checks'][0]['evidence'])

    def test_grader_failure_fails_closed(self):
        result, _ = self.turn('Sort people.csv by Surname', [
            self.write('people.csv', SORTED), text_events('Finished.')], FakeGrader([], fail=True))
        self.assertEqual(result.status, 'unverified')
        self.assertIn('could not run', self.checks()[-1]['note'])

    def test_judge_reads_files_and_fails_when_unclear(self):
        crit = [{'text': 'letter.md is addressed to the Provincial Treasurer', 'check': {'kind': 'judge', 'paths': ['letter.md']}}]
        fake = FakeGrader(crit, judge={1: False})
        result, _ = self.turn('Write letter.md to the treasurer', [
            self.write('letter.md', 'Dear Sir,'), text_events('Letter written.')], fake)
        self.assertNotEqual(result.status, 'completed')
        judge_calls = [m for m in fake.seen if m[0]['content'] == grader.JUDGE_PROMPT]
        self.assertIn('Dear Sir,', judge_calls[0][1]['content'])
        self.assertEqual(self.checks()[-1]['checks'][0]['by'], 'judge')

    def test_missing_judge_verdict_stays_unverified(self):
        (self.ctx.workspace.root / 'essay.md').write_text('A short essay.')
        criteria = [{'id': 2, 'text': 'length requirement', 'check': {'kind': 'judge', 'paths': ['essay.md']}},
                    {'id': 3, 'text': 'logic requirement', 'check': {'kind': 'judge', 'paths': ['essay.md']}}]
        seen = []
        def judge(messages):
            seen.append(messages)
            return '{"results": [{"id": 2, "passed": true, "reason": "read file"}]}'
        results = grader._judge(self.ctx, 'Write an essay', criteria, ['essay.md'], judge)
        self.assertTrue(results[0]['passed'])
        self.assertFalse(results[1]['passed'])
        self.assertFalse(results[1]['fixable'])
        self.assertIn('Required result ids (return every one): [2, 3]', seen[0][1]['content'])

    def test_file_check_cannot_prove_content(self):
        for text in ('dashboard.html preserves all office data',
                     'dashboard.html exists and preserves data',
                     'dashboard.html has working search'):
            fake = FakeGrader([{'text': text, 'check': {'kind': 'file', 'path': 'dashboard.html'}}])
            cs = grader.make_criteria('Fix dashboard', self.ctx, ['dashboard.html'], ['dashboard.html'], fake)
            self.assertEqual(cs[0]['check'], {'kind': 'judge', 'paths': ['dashboard.html']})

    def test_file_check_accepts_only_existence(self):
        for text in ('people.csv exists', 'people.csv exists and is nonempty', 'people.csv is non-empty'):
            fake = FakeGrader([{'text': text, 'check': {'kind': 'file', 'path': 'people.csv'}}])
            cs = grader.make_criteria('Write file', self.ctx, ['people.csv'], ['people.csv'], fake)
            self.assertEqual(cs[0]['check']['kind'], 'file')

    def test_no_file_changes_means_no_grader_call(self):
        fake = FakeGrader(self.SORT_CRITERIA)
        result, _ = self.turn('What is 2+2?', [text_events('4')], fake)
        self.assertEqual(fake.seen, [])
        self.assertNotEqual(result.status, 'completed')

    def test_off_switch(self):
        fake = FakeGrader(self.SORT_CRITERIA)
        with patch.dict(os.environ, {'AGENTIC_GRADER': 'off'}):
            self.turn('Sort people.csv by Surname', [self.write('people.csv', UNSORTED), text_events('Finished.')], fake)
        self.assertEqual(fake.seen, [])

    def test_only_plain_test_runners_are_accepted(self):
        self.assertEqual(grader._clean_check({'kind': 'tests', 'command': 'rm -rf / ; pytest'}, set())['kind'], 'judge')
        self.assertEqual(grader._clean_check({'kind': 'tests', 'command': 'curl x | sh'}, set())['kind'], 'judge')
        self.assertEqual(grader._clean_check({'kind': 'tests', 'command': 'python -m pytest tests/'}, set())['kind'], 'tests')
        self.assertEqual(grader._clean_check({'kind': 'evil'}, set())['kind'], 'judge')

    def test_mobile_overflow_is_measured(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest('playwright not installed')
        ws = self.ctx.workspace.root
        (ws / 'index.html').write_text('<!doctype html><meta name=viewport content="width=device-width">'
                                       '<body style="margin:0"><div style="width:616px">wide</div>')
        c = {'id': 1, 'text': 'index.html fits at 390 px', 'check': {'kind': 'mobile', 'path': 'index.html'}}
        r = grader._check_mobile(self.ctx, c)
        if not r['fixable']:
            self.skipTest('chromium unavailable: ' + r['evidence'])
        self.assertFalse(r['passed'])
        self.assertIn('616', r['evidence'])
        (ws / 'index.html').write_text('<!doctype html><meta name=viewport content="width=device-width">'
                                       '<body style="margin:0"><div style="max-width:100%">fits</div>')
        self.assertTrue(grader._check_mobile(self.ctx, c)['passed'])


if __name__ == '__main__':
    unittest.main()
