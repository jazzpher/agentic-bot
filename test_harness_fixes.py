"""Harness fixes: owner acceptance check, kernel network-off, runtime-safe pip, needs_input, fetch_url hint."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from agenticbot import agent as agent_core, completion, sandbox
from agenticbot.approvals import SandboxPolicy
from agenticbot.tools import ToolContext, default_registry
from agent_loop import Workspace
from test_agenticbot_agent import scripted, text_events, tool_call_events


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.registry = default_registry()
        self.events = []
        self.cfg = ('key', 'https://api.example.test/v1/chat/completions', 'model-x')

    def ctx(self, **pol):
        return ToolContext(workspace=self.ws, session_id='s1',
                           policy=SandboxPolicy(mode='full-access', approval='never', **pol))

    def turn(self, stream_fn, message='do it', ctx=None, **kw):
        return agent_core.run_turn([{'role': 'user', 'content': message}], self.registry, ctx or self.ctx(),
                                   self.cfg, self.events.append, stream_fn=stream_fn, **kw)


class AcceptanceCheckTests(Base):
    def test_passing_owner_check_marks_completed(self):
        fn = scripted([tool_call_events('write_file', {'path': 'out.txt', 'content': '158\n'}), text_events('done')])
        r = self.turn(fn, verify_request=True, requirements=['Owner acceptance command'],
                      completion_verifier=completion.command_verifier('grep -qx 158 out.txt'))
        self.assertEqual(r.status, 'completed')

    def test_failing_owner_check_is_incomplete_not_done(self):
        fn = scripted([tool_call_events('write_file', {'path': 'out.txt', 'content': '157\n'}), text_events('done')])
        r = self.turn(fn, verify_request=True, requirements=['Owner acceptance command'],
                      completion_verifier=completion.command_verifier('grep -qx 158 out.txt'))
        self.assertEqual(r.status, 'incomplete')
        self.assertTrue(any(e.get('type') == 'completion_check' and not e.get('passed') for e in self.events))

    def test_without_check_stays_unverified(self):
        r = self.turn(scripted([text_events('done')]), verify_request=True)
        self.assertEqual(r.status, 'unverified')


@unittest.skipUnless(sandbox.network_namespace_supported(), 'needs Linux user+net namespaces')
class NetworkOffTests(Base):
    PROBE = ("python3 -c \"import socket;s=socket.socket();s.settimeout(3);"
             "print('ERR' if s.connect_ex(('1.1.1.1',443)) else 'CONNECTED')\"")

    def test_network_off_is_kernel_enforced_for_any_binary(self):
        r = sandbox.SubprocessBackend().run(self.PROBE, self.ws.root, 15, network=False)
        self.assertIn('ERR', r.stdout)
        self.assertNotIn('CONNECTED', r.stdout)

    def test_workspace_writes_keep_host_ownership(self):
        sandbox.SubprocessBackend().run('echo hi > f.txt', self.ws.root, 10, network=False)
        self.assertEqual((self.ws.root / 'f.txt').stat().st_uid, os.getuid())


class EnvironmentTests(unittest.TestCase):
    def test_bot_venv_is_not_on_command_path_and_pip_needs_a_venv(self):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, 'botvenv')
            old = dict(os.environ)
            try:
                os.environ['VIRTUAL_ENV'] = fake
                os.environ['PATH'] = os.path.join(fake, 'bin') + os.pathsep + '/usr/bin:/bin'
                env = sandbox.command_environment(d)
            finally:
                os.environ.clear(); os.environ.update(old)
            self.assertNotIn(os.path.join(fake, 'bin'), env['PATH'].split(os.pathsep))
            self.assertEqual(env['PIP_REQUIRE_VIRTUALENV'], 'true')

    def test_workspace_venv_goes_first(self):
        with tempfile.TemporaryDirectory() as d:
            local = Path(d, '.venv', 'Scripts' if os.name == 'nt' else 'bin'); local.mkdir(parents=True)
            self.assertEqual(sandbox.command_environment(d)['PATH'].split(os.pathsep)[0], str(local))


class NeedsInputTests(Base):
    def test_ask_user_ends_turn_as_needs_input(self):
        fn = scripted([tool_call_events('ask_user', {'question': 'Where is the budget sheet?'}), text_events('never')])
        r = self.turn(fn, verify_request=True)
        self.assertEqual(r.status, 'needs_input')
        self.assertIn('budget sheet', r.text)
        self.assertEqual(len(fn.calls), 1)


class FetchHintTests(Base):
    def test_failed_network_command_points_to_fetch_url(self):
        out = self.registry.execute(self.ctx(), 'run_command', {'command': 'curl -sS -m 3 https://example.invalid'})
        self.assertIn('fetch_url', out.output)


if __name__ == '__main__':
    unittest.main()


class AskUserNudgeTest(unittest.TestCase):
    def test_detects_trailing_question_outside_code(self):
        from agenticbot.agent import _asks_user
        self.assertTrue(_asks_user('No budget sheet here.\nCould you share it?'))
        self.assertFalse(_asks_user('Done.\n```\nprint("ok?")\n```'))
        self.assertFalse(_asks_user('Fixed calc.py; add(2, 3) returns 5.'))
