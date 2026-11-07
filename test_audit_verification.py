"""Normal-entrypoint completion evidence must be bounded and honest."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_loop import Workspace
from grokbot import agent, completion
from grokbot.approvals import SandboxPolicy
from grokbot.tools import ToolContext, default_registry
from test_grokbot_agent import scripted, text_events


class AutomaticAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = ToolContext(workspace=Workspace(Path(self.tmp.name) / 'ws'), session_id='audit',
                               policy=SandboxPolicy(mode='workspace-write'))
        self.events = []

    def turn(self, request, answer='done', messages=None):
        return agent.run_turn(messages or [{'role': 'user', 'content': request}], default_registry(), self.ctx,
                              ('key', 'https://api.example.test/v1/chat/completions', 'test'), self.events.append,
                              stream_fn=scripted([text_events(answer)]), verify_request=True)

    def check(self, request, answer='done'):
        return completion.automatic_check({'request': request}, self.ctx, answer)

    def test_normal_turn_is_unverified_not_model_asserted_completed(self):
        result = self.turn('Research the latest census', 'I independently verified everything.')
        self.assertEqual(result.status, 'unverified')
        self.assertEqual(self.events[-1]['status'], 'unverified')
        self.assertEqual([e for e in self.events if e['type'] == 'completion_check'][0]['coverage'], 'limited')

    def test_missing_output_is_incomplete(self):
        result = self.turn('Create report.csv with three rows.')
        self.assertEqual(result.status, 'incomplete')
        self.assertIn('Output file: report.csv', [e for e in self.events if e['type'] == 'completion_check'][0]['missing'])

    def test_nonempty_file_never_proves_content_correctness(self):
        self.ctx.workspace.path('report.csv').write_text('garbage')
        check = self.check('Create report.csv with correct arithmetic.')
        self.assertTrue(check['passed'])
        self.assertEqual(check['status'], 'unverified')
        self.assertIn('not checked', check['checks'][0]['evidence'])

    def test_exact_title_missing_detected(self):
        self.assertEqual(self.turn('Use the title "Quezon Census"', 'Census report').status, 'incomplete')
        self.assertEqual(self.turn('Use the title “Quezon Census”', 'Quezon Census').status, 'unverified')

    def test_file_title_read_and_traversal_fails_closed(self):
        self.ctx.workspace.path('report.md').write_text('# Quezon Census\n')
        self.assertTrue(self.check('Create report.md with title "Quezon Census"')['passed'])
        self.assertFalse(self.check('Create ../report.csv')['passed'])

    def test_existing_read_inputs_not_treated_as_outputs(self):
        self.assertEqual(self.check('Read data.csv and summarize it')['checks'], [])

    def test_resume_keeps_original_and_new_request_resets_it(self):
        first = self.turn('Create missing.csv')
        continued = self.turn('', messages=first.messages + [{'role': 'user', 'content': 'continue'}])
        self.assertEqual(continued.status, 'incomplete')
        fresh = self.turn('', messages=first.messages + [{'role': 'user', 'content': 'hello'}])
        self.assertEqual(fresh.status, 'unverified')
        self.assertEqual(fresh.messages[0][completion.KEY]['request'], 'hello')

    def test_normal_verdict_never_enables_skill_learning(self):
        self.turn('hello')
        self.assertFalse(self.ctx.extras.get('_skill_learning_enabled', False))

    def test_real_cli_entrypoint_uses_gate(self):
        from grokbot.cli import main
        import io
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(['exec', '--mock', '--workspace', str(self.ctx.workspace.root), '--json', 'Create missing.csv'])
        self.assertEqual(code, 1)
        self.assertIn('"status": "incomplete"', out.getvalue())

    def test_real_web_entrypoint_persists_verification(self):
        import webui
        from grokbot.conversations import ConversationStore
        store = ConversationStore(Path(self.tmp.name) / 'conv.sqlite3')
        with patch.dict(webui.app.config, {'MOCK': True, 'API_KEY': '', 'API_URL': '', 'MODEL': 'test'}), \
             patch.object(webui, 'conversation_store', return_value=store):
            cid = store.create('audit', 'audit')
            result, _ = webui.run_owner_turn('audit', self.ctx.workspace, cid, 'Create missing.csv', emit=self.events.append, stop=__import__('threading').Event())
            self.assertEqual(result.status, 'incomplete')
            entries = store.entries(cid, 'audit')
            events = [ev for e in entries for ev in (e.get('events') or [])]
            self.assertTrue(any(ev.get('type') == 'completion_check' for ev in events), entries[-1:])


class CspAllowsInlinePreviewImages(unittest.TestCase):
    """Browser previews arrive as data:image URLs; a CSP without img-src data: renders them broken."""
    def test_img_src_allows_data_urls(self):
        src = Path(__file__).with_name('webui.py').read_text()
        self.assertIn("img-src 'self' data: blob:", src)
