"""Hard-kill recovery: a turn's progress must survive the process dying mid-turn (review finding #1).

A child process runs a real web owner turn (webui.run_owner_turn) against a SQLite conversation store.
Step 1 writes a file (completed batch). Step 2 calls a mutating tool that records it started and then hangs.
The parent SIGKILLs the child mid-tool (no cleanup, no finally), reopens the store and checks that
the completed batch was persisted, the interrupted call is recorded as outcome-unknown (valid tool pairing),
and a recovery turn does not replay the interrupted side effect on its own."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

CHILD = textwrap.dedent('''
    import json, sys, threading, time
    from pathlib import Path
    from unittest.mock import patch
    root = Path(sys.argv[1])
    import webui
    from agent_loop import Workspace
    from agenticbot.approvals import SandboxPolicy
    from agenticbot.conversations import ConversationStore
    from agenticbot.tools import ToolSpec

    def hang(ctx):
        with open(root / 'side_effect.log', 'a') as fh:
            fh.write('ran\\n')
        (root / 'started').write_text('1')
        time.sleep(600)
        return 'never'
    webui.REGISTRY.register(ToolSpec('synthetic_slow_write', 'Synthetic mutating tool for the kill test.',
                                     {'type': 'object', 'properties': {}}, hang, mutates=True))
    script = [
        [{'type': 'tool_calls', 'calls': [{'id': 'c1', 'name': 'write_file',
          'arguments': json.dumps({'path': 'step1.txt', 'content': 'synthetic step one'})}]},
         {'type': 'finish', 'reason': 'tool_calls'}],
        [{'type': 'tool_calls', 'calls': [{'id': 'c2', 'name': 'synthetic_slow_write', 'arguments': '{}'}]},
         {'type': 'finish', 'reason': 'tool_calls'}],
    ]
    calls = []
    def stream_fn(cfg, messages, tools=None, stop=None, **kw):
        calls.append(1)
        for event in script[min(len(calls) - 1, len(script) - 1)]:
            yield event
    store = ConversationStore(root / 'conv.sqlite3')
    cid = (root / 'cid').read_text()
    with patch.object(webui, 'conversation_store', return_value=store), \\
         patch.object(webui, 'agent_stream_fn', stream_fn), \\
         patch.object(webui, 'make_summarizer', lambda cfg: None):
        webui.run_owner_turn('killtest', Workspace(root / 'ws'), cid, 'Write step1.txt then run the slow write',
                             emit=lambda e: None, stop=threading.Event(),
                             policy=SandboxPolicy(mode='workspace-write'),
                             cfg=('synthetic-key', 'https://api.example.test/v1/chat/completions', 'model-x'))
''')


class HardKillCheckpointTest(unittest.TestCase):
    def test_sigkill_mid_turn_keeps_progress_and_does_not_replay(self):
        from agenticbot.conversations import ConversationStore
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ConversationStore(root / 'conv.sqlite3')
            self.addCleanup(store.close)
            cid = store.create('killtest', 'kill test')
            (root / 'cid').write_text(cid)
            child = root / 'child.py'
            child.write_text(CHILD)
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent))
            proc = subprocess.Popen([sys.executable, str(child), str(root)], env=env,
                                    cwd=str(Path(__file__).resolve().parent),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                deadline = time.time() + 60
                while not (root / 'started').exists():
                    if proc.poll() is not None:
                        self.fail('child exited early: ' + proc.stdout.read().decode(errors='replace')[-2000:])
                    if time.time() > deadline:
                        self.fail('child never reached the slow tool')
                    time.sleep(0.05)
                proc.kill()                               # SIGKILL on POSIX: no finally, no cleanup
                proc.wait(10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.stdout.close()
            self.assertNotEqual(proc.returncode, 0)

            fresh = ConversationStore(root / 'conv.sqlite3')
            self.addCleanup(fresh.close)
            context, _ = fresh.get_context(cid, 'killtest')
            roles = [m['role'] for m in context]
            self.assertEqual(roles, ['user', 'assistant', 'tool', 'assistant', 'tool'], context)
            # Completed batch survived with its real result.
            self.assertEqual(context[2]['tool_call_id'], 'c1')
            self.assertIn('UNTRUSTED TOOL RESULT (write_file)', context[2]['content'])
            self.assertEqual((root / 'ws' / 'step1.txt').read_text(), 'synthetic step one')
            # Interrupted call is paired with an explicit unknown outcome (valid native history).
            self.assertEqual(context[3]['tool_calls'][0]['id'], 'c2')
            self.assertEqual(context[4]['tool_call_id'], 'c2')
            self.assertIn('outcome unknown', context[4]['content'])
            self.assertIn('Do not repeat a side effect', context[4]['content'])

            # Recovery: the next turn starts from the checkpoint and the harness replays nothing itself.
            from agent_loop import Workspace
            from agenticbot import agent as agent_core
            from agenticbot.approvals import SandboxPolicy
            from agenticbot.tools import ToolContext, ToolSpec, default_registry
            registry = default_registry()
            reruns = []
            registry.register(ToolSpec('synthetic_slow_write', 'x', {'type': 'object', 'properties': {}},
                                       lambda ctx: reruns.append(1) or 'ran', mutates=True))
            seen = []

            def stream_fn(cfg, messages, tools=None, stop=None, **kw):
                seen.append(messages)
                yield {'type': 'text', 'delta': 'The slow write outcome is unknown; not repeating it.'}
                yield {'type': 'finish', 'reason': 'stop'}
            ctx = ToolContext(workspace=Workspace(root / 'ws'), session_id='killtest',
                              policy=SandboxPolicy(mode='workspace-write'))
            result = agent_core.run_turn(context + [{'role': 'user', 'content': 'continue'}], registry, ctx,
                                         ('k', 'https://api.example.test/v1/chat/completions', 'm'),
                                         lambda e: None, stream_fn=stream_fn)
            self.assertEqual(reruns, [])
            self.assertEqual((root / 'side_effect.log').read_text(), 'ran\n')
            sent = json.dumps(seen[0])
            self.assertIn('outcome unknown', sent)
            self.assertNotEqual(result.status, 'error')


if __name__ == '__main__':
    unittest.main()
