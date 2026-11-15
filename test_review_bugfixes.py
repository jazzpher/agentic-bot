"""Regression tests for the confirmed external-review bugs (synthetic data only).

Covers: loop guard on large/ignored workspaces, empty reply never reported completed,
unattended network policy, POSIX launch without preexec_fn, /notifications cursor,
busy sessions answering 503, and the documentation values.
"""
import os
import pathlib
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

from agenticbot import approvals, config, loopguard
from agenticbot.approvals import SandboxPolicy


class LoopGuardLargeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def test_ignored_directories_do_not_count_towards_the_limit(self):
        for i in range(loopguard.FINGERPRINT_LIMIT + 500):
            p = self.root / 'node_modules' / ('pkg%d' % (i % 50)) / ('f%d.js' % i)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('x')
        (self.root / 'a.txt').write_text('alpha')
        self.assertIsNotNone(loopguard.workspace_fingerprint(str(self.root)),
                             'node_modules must not push the tree past the limit')

    def test_a_workspace_over_the_limit_still_fingerprints(self):
        for i in range(loopguard.FINGERPRINT_LIMIT + 50):
            (self.root / ('f%05d.txt' % i)).write_text('x')
        first = loopguard.workspace_fingerprint(str(self.root))
        self.assertIsNotNone(first, 'repeat protection must not switch itself off on a big tree')
        (self.root / 'f00000.txt').write_text('changed')
        self.assertNotEqual(loopguard.workspace_fingerprint(str(self.root)), first)

    def test_repeat_detection_works_in_a_large_workspace(self):
        from agenticbot.tools import ToolResult
        for i in range(loopguard.FINGERPRINT_LIMIT + 50):
            (self.root / ('f%05d.txt' % i)).write_text('x')
        guard = loopguard.ProgressGuard(type('W', (), {'root': str(self.root)})(), repeats=2)
        call = {'name': 'read_file', 'arguments': '{"path": "f00001.txt"}'}
        registry = mock.Mock()
        registry.get.return_value = None
        result = ToolResult('read_file', {}, 'x', True, 0.0)
        for _ in range(2):
            self.assertEqual(guard.check_batch([call], registry), [None])
            guard.observe([call], [result])
        self.assertTrue(guard.check_batch([call], registry)[0], 'third identical call must be blocked')

    def test_skipped_directories_match_the_grader(self):
        for name in ('.git', '.venv', 'node_modules', 'dist', 'build'):
            self.assertIn(name, loopguard.SKIP_DIRS)


class EmptyReplyTests(unittest.TestCase):
    def test_empty_reply_is_not_completed(self):
        from agenticbot import agent as agent_core
        from agenticbot.providers import mock_stream
        from agenticbot.tools import default_registry
        from agent_loop import Workspace

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = Workspace(pathlib.Path(tmp.name) / 'ws')
        events = []

        def silent(messages, model, tools=None, **kw):
            yield {'type': 'text', 'delta': ''}
            yield {'type': 'finish', 'reason': 'stop'}

        from agenticbot.tools import ToolContext
        ctx = ToolContext(workspace=ws, session_id='s', policy=SandboxPolicy(mode='workspace-write'))
        result = agent_core.run_turn([{'role': 'user', 'content': 'say nothing'}], default_registry(), ctx,
                                     ('', '', 'mock'), events.append, stream_fn=silent)
        self.assertNotEqual(result.status, 'completed', 'an empty reply must never report completed')
        self.assertEqual(result.status, 'error')
        self.assertTrue(any(e.get('type') == 'error' for e in events))


class UnattendedNetworkTests(unittest.TestCase):
    def test_unattended_turns_network_off(self):
        policy = SandboxPolicy(mode='full-access', approval='on-request', network=True)
        self.assertFalse(policy.for_unattended().network)
        self.assertFalse(policy.for_unattended(allow_full_access=True).network)

    def test_network_needs_an_explicit_opt_in(self):
        policy = SandboxPolicy(mode='workspace-write', network=True)
        self.assertTrue(policy.for_unattended(allow_network=True).network)
        self.assertFalse(SandboxPolicy(mode='workspace-write', network=False)
                         .for_unattended(allow_network=True).network)

    def test_unattended_never_waits_for_approval(self):
        policy = SandboxPolicy(mode='full-access', approval='on-request', network=True).for_unattended()
        self.assertEqual(policy.approval, 'never')
        self.assertEqual(policy.mode, 'workspace-write')
        self.assertFalse(policy.yolo)


@unittest.skipUnless(os.name == 'posix', 'POSIX launch path')
class PosixLaunchTests(unittest.TestCase):
    def test_shell_runs_without_preexec_fn(self):
        from agenticbot import sandbox
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = pathlib.Path(tmp.name)
        seen = {}
        real_popen = sandbox.subprocess.Popen

        def spy(*a, **kw):
            seen.update(kw)
            return real_popen(*a, **kw)

        with mock.patch.object(sandbox.subprocess, 'Popen', side_effect=spy):
            out = sandbox.SubprocessBackend().run('echo hello', cwd=ws, timeout=10)
        self.assertNotIn('preexec_fn', seen, 'no Python work may run in the forked child')
        self.assertTrue(seen.get('start_new_session'))
        self.assertIn('hello', out.stdout)

    def test_limits_are_applied_after_exec(self):
        from agenticbot import sandbox
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = sandbox.SubprocessBackend().run('ulimit -f; ulimit -t', cwd=pathlib.Path(tmp.name), timeout=10)
        self.assertEqual(out.exit_code, 0, out.stderr)
        self.assertNotIn('unlimited', out.stdout.split('\n')[0])


class NotificationCursorTests(unittest.TestCase):
    def setUp(self):
        import webui
        self.webui = webui
        webui.app.config.update(TESTING=True)
        self.c = webui.app.test_client()
        self.c.get('/')

    def test_two_readers_both_see_the_notification(self):
        self.webui.notify('owner-a', {'title': 'Ping', 'conversation_id': 'c1'})
        with self.c.session_transaction() as s:
            s['sid'] = 'owner-a'
        first = self.c.get('/notifications').get_json()
        second = self.c.get('/notifications').get_json()
        self.assertEqual([n['title'] for n in first], ['Ping'])
        self.assertEqual([n['title'] for n in second], ['Ping'], 'a second tab must not lose it')

    def test_cursor_returns_only_newer_ones(self):
        self.webui.notify('owner-b', {'title': 'One', 'conversation_id': 'c1'})
        with self.c.session_transaction() as s:
            s['sid'] = 'owner-b'
        first = self.c.get('/notifications').get_json()
        cursor = first[-1]['time']
        self.assertEqual(self.c.get('/notifications?since=%s' % cursor).get_json(), [])
        self.webui.notify('owner-b', {'title': 'Two', 'conversation_id': 'c2'})
        fresh = self.c.get('/notifications?since=%s' % cursor).get_json()
        self.assertEqual([n['title'] for n in fresh], ['Two'])

    def test_old_notifications_are_pruned(self):
        self.webui.notify('owner-c', {'title': 'Stale', 'conversation_id': 'c1'})
        with self.c.session_transaction() as s:
            s['sid'] = 'owner-c'
        self.webui.NOTIFICATIONS['owner-c'][0]['time'] = time.time() - self.webui.NOTIFICATION_TTL - 5
        self.assertEqual(self.c.get('/notifications').get_json(), [])


class BusySessionTests(unittest.TestCase):
    def test_busy_sessions_answer_503(self):
        import webui
        webui.app.config.update(TESTING=True)
        c = webui.app.test_client()
        c.get('/')
        with mock.patch.object(webui, 'client', side_effect=webui.SessionsBusy('All sessions busy.')):
            res = c.post('/agent', json={'message': 'hi'})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.headers.get('Retry-After'), '5')


class DocumentationTests(unittest.TestCase):
    def test_documented_values_match_the_code(self):
        readme = pathlib.Path('README.md').read_text()
        self.assertIn('AGENTIC_MAX_STEPS` (default %d)' % config.MAX_STEPS, readme)
        limits = pathlib.Path('docs/LIMITATIONS.md').read_text()
        from agenticbot import memory
        self.assertIn('%d chars' % memory.MAX_INJECT_CHARS, limits)
        arch = pathlib.Path('docs/ARCHITECTURE.md').read_text()
        self.assertIn('%d%% full' % round(config.COMPACT_RATIO * 100), arch)


if __name__ == '__main__':
    unittest.main()
