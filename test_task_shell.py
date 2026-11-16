"""run_command inside the durable task loop: opt-in, bounded, network-off, stoppable.

The durable harness has no approval prompt, so shell access is the owner's decision
(AGENTIC_TASK_SHELL), never the model's. These tests prove that a model cannot turn
it on, that a refused command never reaches the backend, that the remaining elapsed
budget caps the timeout, and that a command honestly invalidates earlier read-back.
"""
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_loop import ACTION_FIELDS, ACTION_SCHEMA, ToolError, Workspace, parse_action
from agenticbot import config, sandbox
from task_harness import TaskManager


def action(kind, **kw):
    return json.dumps({'action': kind, **kw})


class FakeBackend:
    """Records the invocation instead of running anything; optionally writes a file."""

    name = 'fake'
    isolated = False

    def __init__(self, exit_code=0, stdout='ok\n', available=True, touch=None):
        self.calls = []
        self.exit_code = exit_code
        self.stdout = stdout
        self.available_value = available
        self.touch = touch

    def available(self):
        return self.available_value

    def run(self, command, cwd, timeout, network=False, read_only=False, stop=None):
        self.calls.append({'command': command, 'cwd': cwd, 'timeout': timeout,
                           'network': network, 'read_only': read_only, 'stop': stop})
        if self.touch:
            (pathlib.Path(cwd) / self.touch[0]).write_text(self.touch[1], encoding='utf-8')
        return sandbox.CommandResult(self.stdout, '', self.exit_code, 0.0,
                                     backend=self.name, isolated=False)


class WorkspaceShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(pathlib.Path(self.tmp.name) / 'ws')
        self.backend = FakeBackend()
        self.ws.backend = self.backend
        patcher = mock.patch.object(sandbox, 'get_backend', lambda *a, **k: self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def enable(self, on=True):
        patcher = mock.patch.object(config, 'TASK_SHELL', on)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_off_by_default_is_a_refusal_not_a_command(self):
        with self.assertRaises(ToolError) as ctx:
            self.ws.execute({'action': 'run_command', 'command': 'echo hi'})
        self.assertIn('AGENTIC_TASK_SHELL', str(ctx.exception))
        self.assertEqual(self.backend.calls, [])

    def test_action_and_schema_carry_the_command_field(self):
        self.assertEqual(ACTION_FIELDS['run_command'], ('command',))
        self.assertIn('command', ACTION_SCHEMA['properties'])
        self.assertIn('command', ACTION_SCHEMA['required'])
        with self.assertRaises(ToolError):
            parse_action(action('run_command', command='   '))

    def test_always_refused_command_never_reaches_the_backend(self):
        self.enable()
        with self.assertRaises(ToolError) as ctx:
            self.ws.execute({'action': 'run_command', 'command': 'rm -rf /'})
        self.assertIn('Refused', str(ctx.exception))
        self.assertEqual(self.backend.calls, [])

    def test_runs_in_the_workspace_with_the_network_off(self):
        self.enable()
        output = self.ws.execute({'action': 'run_command', 'command': 'echo hi'})
        self.assertIn('ok', output)
        self.assertIn('exit code 0', output)
        call = self.backend.calls[0]
        self.assertFalse(call['network'])
        self.assertFalse(call['read_only'])
        self.assertEqual(pathlib.Path(call['cwd']), self.ws.root)
        self.assertLessEqual(call['timeout'], config.COMMAND_TIMEOUT)

    def test_timeout_follows_the_remaining_elapsed_budget(self):
        self.enable()
        self.ws.command_cap = 3
        self.ws.execute({'action': 'run_command', 'command': 'echo hi'})
        self.assertEqual(self.backend.calls[0]['timeout'], 3)

    def test_unavailable_backend_is_an_error(self):
        self.enable()
        self.ws.backend = FakeBackend(available=False)
        with self.assertRaises(ToolError) as ctx:
            self.ws.execute({'action': 'run_command', 'command': 'echo hi'})
        self.assertIn('backend', str(ctx.exception))

    def test_a_command_invalidates_earlier_read_back(self):
        self.enable()
        self.ws.execute({'action': 'write_file', 'path': 'a.txt', 'content': 'x'})
        self.ws.execute({'action': 'read_file', 'path': 'a.txt'})
        self.assertEqual(self.ws.checked, {'a.txt'})
        self.ws.execute({'action': 'run_command', 'command': 'echo hi'})
        self.assertEqual(self.ws.checked, set())
        self.assertEqual(self.ws.written, {'a.txt'})

    def test_the_task_stop_event_reaches_the_backend(self):
        self.enable()
        stop = threading.Event()
        self.ws.stop = stop
        self.ws.execute({'action': 'run_command', 'command': 'echo hi'})
        self.assertIs(self.backend.calls[0]['stop'], stop)

    def test_a_stopped_command_says_so(self):
        self.enable()
        self.ws.backend = FakeBackend()
        self.ws.backend.run = lambda command, cwd, timeout, **kw: sandbox.CommandResult(
            '', '', None, 0.4, stopped=True, backend='fake')
        output = self.ws.execute({'action': 'run_command', 'command': 'sleep 30'})
        self.assertIn('stopped', output)


class DurableShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        patcher = mock.patch.object(config, 'TASK_SHELL', True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def manager(self):
        m = TaskManager(self.root / 'tasks.db', self.root / 'workspaces')
        self.addCleanup(m.close)
        return m

    def wait(self, m, tid, owner='alice'):
        end = time.monotonic() + 15
        while time.monotonic() < end:
            row = m.get(tid, owner)
            if row['status'] not in ('queued', 'running'):
                return row
            time.sleep(.01)
        self.fail('worker did not settle')

    def fake_backend(self, **kw):
        backend = FakeBackend(**kw)
        patcher = mock.patch.object(sandbox, 'get_backend', lambda *a, **k: backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        return backend

    def test_task_runs_a_command_reads_back_and_passes_its_check(self):
        backend = self.fake_backend(touch=('answer.txt', '42'))
        m = self.manager()
        replies = iter([action('run_command', command='python make_answer.py'),
                        action('read_file', path='answer.txt'),
                        action('done', text='ran make_answer.py, read answer.txt back: 42')])
        tid = m.submit('alice', 'Produce answer.txt containing 42', [], lambda _m: next(replies),
                       checks=[{'type': 'file_text', 'path': 'answer.txt', 'text': '42'}])
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(backend.calls[0]['command'], 'python make_answer.py')
        self.assertEqual(row['state']['steps'], 3)

    def test_hardline_command_fails_the_action_but_not_the_task(self):
        backend = self.fake_backend()
        m = self.manager()
        replies = iter([action('run_command', command='rm -rf /'),
                        action('done', text='that command is blocked'),
                        action('done', text='nothing to produce; the command was refused')])
        tid = m.submit('alice', 'Try a blocked command', [], lambda _m: next(replies))
        row = self.wait(m, tid)
        self.assertIn(row['status'], ('completed', 'completed_unverified'))
        self.assertEqual(backend.calls, [])
        self.assertTrue(any(e['type'] == 'tool' and 'Refused' in e['text']
                            for e in m.events(tid, 'alice')))

    def test_shell_off_inside_a_task_reports_the_flag(self):
        self.fake_backend()
        off = mock.patch.object(config, 'TASK_SHELL', False)
        off.start()
        self.addCleanup(off.stop)
        m = self.manager()
        replies = iter([action('run_command', command='echo hi'),
                        action('done', text='shell is off'),
                        action('done', text='nothing to produce')])
        tid = m.submit('alice', 'Try the shell', [], lambda _m: next(replies))
        row = self.wait(m, tid)
        self.assertIn('AGENTIC_TASK_SHELL', json.dumps(m.events(tid, 'alice'), ensure_ascii=False))
        self.assertIn(row['status'], ('completed', 'completed_unverified'))


if __name__ == '__main__':
    unittest.main()
