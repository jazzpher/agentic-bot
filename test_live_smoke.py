"""The live NVIDIA check's own logic, driven by a scripted provider so CI runs it.

live_nim_smoke.py costs provider quota and needs a key, so the live job skips
without one. That left its newest phase -- the debug cycle -- unexercised in CI.
These tests drive the same phase_debug() end to end with a scripted model: the
real TaskManager, the real command check through the sandbox backend, the real
acceptance re-run. Only the model is fake.

The point is the negative case: a half-fixed artifact must be rejected, so the
gate is proven to bite rather than merely to pass.
"""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from agenticbot import config


def action(kind, **kw):
    return json.dumps({'action': kind, **kw})


def scripted(replies):
    """A provider that returns the next scripted action, then repeats the last."""
    queue = list(replies)
    calls = []

    def call(_messages, max_output_bytes=4000):
        calls.append(1)
        return queue.pop(0) if len(queue) > 1 else queue[0]
    call.calls = calls
    return call


class DebugCycleTests(unittest.TestCase):
    def setUp(self):
        import live_nim_smoke
        self.smoke = live_nim_smoke
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        # CI does not set AGENTIC_TASK_SHELL; the phase under test needs the shell.
        on = mock.patch.object(config, 'TASK_SHELL', True)
        on.start()
        self.addCleanup(on.stop)

    def run_phase(self, replies):
        # A small elapsed budget keeps a hung phase from eating the job: wait_for
        # gives up 120 s after it, so a broken run fails fast instead of hanging.
        return self.smoke.phase_debug(('fake', 'https://example.invalid/v1', 'fake-model'),
                                      self.root, elapsed_budget=45,
                                      call_model=scripted(replies))

    def test_a_correct_fix_passes_the_command_check(self):
        row, elapsed, fixed, commands, gate = self.run_phase([
            action('run_command', command=self.smoke.DEBUG_COMMAND),
            action('write_file', path='calc.py', content=self.smoke.DEBUG_FIXED),
            action('read_file', path='calc.py'),
            action('done', text='Ran the suite, fixed the offset and the empty list, re-ran it green.'),
        ])
        self.assertEqual(row['status'], 'completed', row.get('state', {}).get('messages'))
        self.assertEqual(gate.exit_code, 0, gate.render())
        self.assertIn('OK', gate.render())
        self.assertEqual(commands, [self.smoke.DEBUG_COMMAND], 'the model must actually run the tests')
        self.assertEqual(fixed, self.smoke.DEBUG_FIXED)

    def test_a_half_fix_is_rejected_by_the_gate(self):
        row, _elapsed, fixed, _commands, gate = self.run_phase([
            action('write_file', path='calc.py', content=self.smoke.DEBUG_HALF_FIXED),
            action('read_file', path='calc.py'),
            action('done', text='Fixed the offset.'),
            action('done', text='Fixed the offset.'),
            action('need_input', text='Stuck: the empty-list case still fails.'),
        ])
        self.assertNotEqual(row['status'], 'completed',
                            'a half-fixed module still fails its tests, so done must not be accepted')
        self.assertNotEqual(gate.exit_code, 0, 'the acceptance command must fail on the half fix')
        self.assertEqual(fixed, self.smoke.DEBUG_HALF_FIXED)

    def test_rewriting_the_specification_does_not_pass(self):
        row, _elapsed, fixed, _commands, _gate = self.run_phase([
            action('write_file', path='test_calc.py', content='import unittest\n'),
            action('read_file', path='test_calc.py'),
            action('done', text='Simplified the tests.'),
            action('done', text='Simplified the tests.'),
            action('need_input', text='Stuck: the pinned specification no longer matches.'),
        ])
        self.assertNotEqual(row['status'], 'completed',
                            'the pinned test file check must reject a rewritten specification')
        self.assertEqual(fixed, self.smoke.DEBUG_SOURCE, 'calc.py was never the problem here')

    def test_the_starting_workspace_really_fails(self):
        from agenticbot import sandbox
        ws = self.root / 'seed'
        ws.mkdir(parents=True, exist_ok=True)
        (ws / 'calc.py').write_text(self.smoke.DEBUG_SOURCE, encoding='utf-8')
        (ws / 'test_calc.py').write_text(self.smoke.DEBUG_TESTS, encoding='utf-8')
        gate = sandbox.get_backend().run(self.smoke.DEBUG_COMMAND, ws, 60, network=False)
        self.assertNotEqual(gate.exit_code, 0, 'the task is pointless unless the tests start red')
        self.assertIn('FAILED', gate.render())


class LongHorizonTests(unittest.TestCase):
    """The long-horizon phase, offline: four bugs, a new file, three gates.

    The gates are the point. A run that fixes stats.py but never creates report.py
    must not reach completed, and the starting workspace must really be red.
    """

    def setUp(self):
        import live_nim_smoke
        self.smoke = live_nim_smoke
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        on = mock.patch.object(config, 'TASK_SHELL', True)
        on.start()
        self.addCleanup(on.stop)

    def run_phase(self, replies):
        return self.smoke.phase_long(('fake', 'https://example.invalid/v1', 'fake-model'),
                                     self.root, elapsed_budget=60,
                                     call_model=scripted(replies))

    def test_a_full_solution_passes_all_three_gates(self):
        # The read-backs come after the last run_command on purpose: a command
        # invalidates earlier reads, so a model that reads before its final test
        # run has to read again. The live model does exactly this.
        row, _elapsed, commands, tests_gate, report_gate = self.run_phase([
            action('write_file', path='stats.py', content=self.smoke.LONG_FIXED),
            action('run_command', command=self.smoke.LONG_COMMAND),
            action('write_file', path='report.py', content=self.smoke.LONG_REPORT_SOURCE),
            action('run_command', command=self.smoke.LONG_REPORT),
            action('read_file', path='stats.py'),
            action('read_file', path='report.py'),
            action('done', text='Fixed four bugs in stats.py, wrote report.py, tests are green.'),
        ])
        self.assertEqual(row['status'], 'completed', row.get('state', {}).get('messages'))
        self.assertEqual(tests_gate.exit_code, 0, tests_gate.render())
        self.assertEqual(report_gate.exit_code, 0, report_gate.render())
        self.assertIn(self.smoke.LONG_OUTPUT, report_gate.render())
        self.assertIn(self.smoke.LONG_COMMAND, commands, 'the model must actually run the tests')

    def test_a_missing_report_py_is_rejected(self):
        row, _elapsed, _commands, _tests_gate, report_gate = self.run_phase([
            action('write_file', path='stats.py', content=self.smoke.LONG_FIXED),
            action('read_file', path='stats.py'),
            action('done', text='Fixed stats.py.'),
            action('done', text='Fixed stats.py.'),
            action('need_input', text='Stuck: report.py was never created.'),
        ])
        self.assertNotEqual(row['status'], 'completed',
                            'the second file is missing, so done must not be accepted')
        self.assertNotEqual(report_gate.exit_code, 0)

    def test_the_starting_workspace_really_fails(self):
        from agenticbot import sandbox
        ws = self.root / 'seed'
        ws.mkdir(parents=True, exist_ok=True)
        (ws / 'stats.py').write_text(self.smoke.LONG_SOURCE, encoding='utf-8')
        (ws / 'test_stats.py').write_text(self.smoke.LONG_TESTS, encoding='utf-8')
        gate = sandbox.get_backend().run(self.smoke.LONG_COMMAND, ws, 60, network=False)
        self.assertNotEqual(gate.exit_code, 0, 'the task is pointless unless the tests start red')


class NoKeyTests(unittest.TestCase):
    def test_config_prefers_the_nvidia_variable(self):
        with mock.patch.dict('os.environ', {'NVIDIA_API_KEY': 'nvapi-x', 'AGENTIC_API_KEY': 'other'}):
            self.smoke_cfg('nvapi-x')
        with mock.patch.dict('os.environ', {'AGENTIC_API_KEY': 'nvapi-y'}, clear=True):
            self.smoke_cfg('nvapi-y')

    def smoke_cfg(self, expected):
        import live_nim_smoke
        self.assertEqual(live_nim_smoke.config()[0], expected)

    def test_no_key_returns_skip_not_failure(self):
        import live_nim_smoke
        with mock.patch.dict('os.environ', {}, clear=True):
            self.assertEqual(live_nim_smoke.config()[0], '')
            with mock.patch('builtins.print'):
                self.assertEqual(live_nim_smoke.main(), 2)


if __name__ == '__main__':
    unittest.main()
