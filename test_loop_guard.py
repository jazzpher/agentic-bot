"""Offline tests for the progress guard (agenticbot/loopguard.py). Synthetic workspaces only."""
import json
from unittest.mock import patch

from agenticbot import config, loopguard
from agenticbot.tools import ToolResult
from test_tool_order_truncation import LoopTestCase, final, scripted, tool_batch


def batch(cid, name, **args):
    return tool_batch((cid, name, args))


class ProgressGuardTurnTests(LoopTestCase):
    def notes(self):
        return [e['text'] for e in self.events if e.get('type') == 'status' and 'Loop guard' in e.get('text', '')]

    def test_identical_read_with_no_change_is_nudged_then_blocked(self):
        self.ws.path('a.txt').write_text('alpha')
        real = self.registry.execute
        with patch.object(self.registry, 'execute', side_effect=real) as execute:
            result = self.turn(scripted([batch('1', 'read_file', path='a.txt'), batch('2', 'read_file', path='a.txt'),
                                         batch('3', 'read_file', path='a.txt'), final('done')]))
        out = self.tool_output(result)
        self.assertNotIn('Loop guard', out['1'])
        self.assertIn('alpha', out['2'])
        self.assertIn('HARNESS NOTE', out['2'])
        self.assertIn('Tool error: Loop guard', out['3'])
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(self.notes()), 1)

    def test_a_real_change_resets_the_repeat_count(self):
        self.ws.path('a.txt').write_text('alpha')
        result = self.turn(scripted([batch('1', 'read_file', path='a.txt'), batch('2', 'read_file', path='a.txt'),
                                     batch('w', 'write_file', path='a.txt', content='beta'),
                                     batch('3', 'read_file', path='a.txt'), batch('4', 'read_file', path='a.txt'),
                                     final('done')]))
        out = self.tool_output(result)
        self.assertIn('beta', out['3'])
        self.assertNotIn('Loop guard', out['3'])
        self.assertIn('HARNESS NOTE', out['4'])

    def test_same_test_run_with_no_edit_between_is_a_loop_even_for_a_mutating_tool(self):
        def execute(ctx, name, args):
            return ToolResult(name, args, 'exit code 1\nFAIL: test_median', True, 0.0)
        cmd = {'command': 'python3 test_calc.py'}
        with patch.object(self.registry, 'execute', side_effect=execute) as ex:
            result = self.turn(scripted([batch('1', 'run_command', **cmd), batch('2', 'run_command', **cmd),
                                         batch('3', 'run_command', **cmd), final('stuck')]))
        out = self.tool_output(result)
        self.assertIn('HARNESS NOTE', out['2'])
        self.assertIn('Tool error: Loop guard: you made this exact call 2 times', out['3'])
        self.assertEqual(ex.call_count, 2)

    def test_same_output_after_the_workspace_changed_is_not_a_loop(self):
        n = [0]

        def execute(ctx, name, args):
            n[0] += 1
            self.ws.path('calc.py').write_text('attempt %d' % n[0])   # the command (or an edit) changed files
            return ToolResult(name, args, 'exit code 1\nFAIL', True, 0.0)
        cmd = {'command': 'python3 test_calc.py'}
        with patch.object(self.registry, 'execute', side_effect=execute) as ex:
            result = self.turn(scripted([batch(str(i), 'run_command', **cmd) for i in range(4)] + [final('x')]))
        self.assertEqual(ex.call_count, 4)
        self.assertFalse(self.notes())
        self.assertNotIn('Loop guard', json.dumps(self.tool_output(result)))

    def test_changing_output_is_not_a_loop(self):
        n = [0]

        def execute(ctx, name, args):
            n[0] += 1
            return ToolResult(name, args, 'tick %d' % n[0], True, 0.0)
        with patch.object(self.registry, 'execute', side_effect=execute) as ex:
            self.turn(scripted([batch(str(i), 'run_command', command='date') for i in range(4)] + [final('x')]))
        self.assertEqual(ex.call_count, 4)
        self.assertFalse(self.notes())

    def test_exact_failed_call_is_blocked_until_something_changes(self):
        result = self.turn(scripted([batch('1', 'read_file', path='missing.txt'),
                                     batch('2', 'read_file', path='missing.txt'),
                                     batch('w', 'write_file', path='missing.txt', content='now here'),
                                     batch('3', 'read_file', path='missing.txt'), final('done')]))
        out = self.tool_output(result)
        self.assertIn('Tool error', out['1'])
        self.assertNotIn('Loop guard', out['1'])
        self.assertIn('Loop guard: this exact call already failed', out['2'])
        self.assertIn('now here', out['3'])

    def test_outside_path_gets_a_hint_and_is_then_refused(self):
        self.ws.path('app.log').write_text('ERROR Timeout\n')
        real = self.registry.execute
        with patch.object(self.registry, 'execute', side_effect=real) as ex:
            result = self.turn(scripted([batch('1', 'read_file', path='/app/app.log'),
                                         batch('2', 'list_files', path='/app/app.log'),
                                         batch('3', 'read_file', path='app.log'), final('done')]))
        out = self.tool_output(result)
        self.assertIn('Path must stay inside this workspace', out['1'])
        self.assertIn('"app.log" is in the workspace: use "app.log"', out['1'])
        self.assertIn('Top-level entries: app.log', out['1'])
        self.assertIn('Tool error: Loop guard: "/app/app.log" already failed', out['2'])
        self.assertIn('ERROR Timeout', out['3'])
        self.assertEqual(ex.call_count, 2)

    def test_no_op_edit_is_flagged_and_does_not_reset_the_repeat_count(self):
        self.ws.path('calc.py').write_text('x = 1\n')
        def execute(ctx, name, args):
            if name == 'edit_file':
                self.ws.path('calc.py').write_text('x = 1\n')     # same text rewritten: mtime moves, content doesn't
                return ToolResult(name, args, 'Edited calc.py (1 replacement, +0 -0 lines).', True, 0.0)
            return ToolResult(name, args, 'exit code 1\nFAIL', True, 0.0)
        edit = {'path': 'calc.py', 'old_text': 'x = 1', 'new_text': 'x = 1'}
        cmd = {'command': 'python3 test_calc.py'}
        with patch.object(self.registry, 'execute', side_effect=execute) as ex:
            result = self.turn(scripted([batch('r1', 'run_command', **cmd), batch('e1', 'edit_file', **edit),
                                         batch('r2', 'run_command', **cmd), batch('e2', 'edit_file', **edit),
                                         batch('r3', 'run_command', **cmd), final('stuck')]))
        out = self.tool_output(result)
        self.assertIn('that call changed nothing', out['e1'])
        self.assertIn('HARNESS NOTE', out['r2'])
        self.assertIn('Tool error: Loop guard', out['r3'])

    def test_real_edit_is_not_flagged(self):
        self.ws.path('calc.py').write_text('x = 1\n')
        result = self.turn(scripted([batch('e', 'edit_file', path='calc.py', old_text='x = 1', new_text='x = 2'),
                                     final('done')]))
        self.assertNotIn('Loop guard', self.tool_output(result)['e'])

    def test_dot_as_a_file_points_to_list_files(self):
        result = self.turn(scripted([batch('1', 'read_file', path='.'), final('done')]))
        self.assertIn('Use list_files', self.tool_output(result)['1'])

    def test_two_blocked_steps_still_end_the_turn(self):
        self.ws.path('a.txt').write_text('alpha')
        result = self.turn(scripted([batch('1', 'read_file', path='a.txt')]))   # repeats forever
        self.assertEqual(result.status, 'error')
        self.assertLessEqual(result.steps, 5)

    def test_guard_can_be_turned_off(self):
        self.ws.path('a.txt').write_text('alpha')
        real = self.registry.execute
        with patch.object(config, 'LOOP_GUARD', 'off'), \
                patch.object(self.registry, 'execute', side_effect=real) as ex:
            result = self.turn(scripted([batch(str(i), 'read_file', path='a.txt') for i in range(3)] + [final('x')]))
        self.assertEqual(ex.call_count, 3)
        self.assertNotIn('Loop guard', json.dumps(self.tool_output(result)))


class FingerprintTests(LoopTestCase):
    def test_caches_are_ignored_and_real_files_count(self):
        root = str(self.ws.root)
        self.ws.path('calc.py').write_text('x = 1')
        first = loopguard.workspace_fingerprint(root)
        cache = self.ws.root / '__pycache__'
        cache.mkdir()
        (cache / 'calc.cpython-312.pyc').write_bytes(b'\0')
        (self.ws.root / 'stray.pyc').write_bytes(b'\0')
        self.assertEqual(first, loopguard.workspace_fingerprint(root))
        self.ws.path('calc.py').write_text('x = 22')
        self.assertNotEqual(first, loopguard.workspace_fingerprint(root))

    def test_too_big_or_missing_is_unknown_and_never_blocks(self):
        self.ws.path('a.txt').write_text('a')
        self.ws.path('b.txt').write_text('b')
        self.assertIsNone(loopguard.workspace_fingerprint(str(self.ws.root), limit=1))
        self.assertIsNone(loopguard.workspace_fingerprint(str(self.ws.root / 'nope')))
        guard = loopguard.ProgressGuard(None)
        call = {'name': 'read_file', 'arguments': '{"path": "a.txt"}'}
        res = ToolResult('read_file', {}, 'same', True, 0.0)
        for _ in range(4):
            self.assertEqual(guard.check_batch([call], self.registry), [None])
            guard.observe([call], [res])
