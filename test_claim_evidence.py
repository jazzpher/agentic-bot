import json
import tempfile
import unittest
from pathlib import Path
from agent_loop import Workspace
from agenticbot import evidence, agent
from agenticbot.tools import ToolContext, default_registry
from test_agenticbot_agent import scripted, text_events

class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = ToolContext(workspace=Workspace(Path(self.tmp.name)), session_id='test', turn_id='t')
        self.reg = default_registry(plugins_too=False)
    def write(self, name, text):
        self.ctx.workspace.path(name).write_text(text)
    def sort(self):
        return self.reg.execute(self.ctx, 'verify_sort', {'path':'names.csv','column':'Surname'})
    def test_accent_inversion_and_final_artifact(self):
        self.write('names.csv', 'Surname\nReyes\nÁlvarez\n')
        self.assertFalse(json.loads(self.sort().output)['passed'])
        self.assertTrue(evidence.issues(self.ctx, 'names.csv is sorted A–Z.'))
        self.write('names.csv', 'Surname\nÁlvarez\nReyes\n')
        self.assertTrue(json.loads(self.sort().output)['passed'])
        self.assertFalse(evidence.issues(self.ctx, 'names.csv is sorted A–Z.'))
        self.write('names.csv', 'Surname\nReyes\nÁlvarez\n')
        self.assertTrue(evidence.issues(self.ctx, 'names.csv is sorted A–Z.'))
    def test_wrong_file_cannot_borrow_receipt(self):
        self.write('names.csv', 'Surname\nÁlvarez\nReyes\n'); self.sort()
        self.write('wrong.csv', 'Surname\nReyes\nÁlvarez\n')
        self.assertTrue(evidence.issues(self.ctx, 'wrong.csv is sorted A–Z.'))
        self.assertTrue(evidence.issues(self.ctx, 'Sorted A–Z.'))
    def test_blanket_checks_and_caveats(self):
        for text in ('All checks passed.', 'Logic verified.', 'Self-check: pass.', 'Everything checked. All tests passed.'):
            self.assertTrue(evidence.issues(self.ctx, text), text)
        self.assertFalse(evidence.issues(self.ctx, 'Mobile layout is not verified.'))
        self.assertTrue(evidence.issues(self.ctx, 'No overflow, but research was not available.'))
    def test_mobile_receipt_invalidated_by_css(self):
        self.write('index.html', '<h1>Hello</h1>'); self.write('style.css', 'h1{color:red}')
        evidence.record(self.ctx, 'mobile','index.html',True,dependencies=evidence.visual_digest(self.ctx))
        self.assertFalse(evidence.issues(self.ctx, 'index.html fits mobile.'))
        self.write('style.css', 'h1{width:900px}')
        self.assertTrue(evidence.issues(self.ctx, 'index.html fits mobile.'))
    def test_rejected_claim_never_reaches_text_stream(self):
        events=[]
        result=agent.run_turn([{'role':'user','content':'Check essay logic'}],self.reg,self.ctx,
            ('key','https://example.test/v1/chat/completions','test'),events.append,
            stream_fn=scripted([text_events('All checks passed.')]),verify_request=True)
        self.assertEqual(result.status,'incomplete')
        self.assertNotIn('All checks passed', ''.join(e.get('delta','') for e in events))
    def test_reading_source_is_not_verification(self):
        # Exact sentence from the 14 Nov preview-off Nemotron run that slipped through PR #68.
        bad = ("preview_page isn't available in this environment, so I verified by reviewing the CSS directly.",
               'Checked by reading the HTML, it looks fine.', 'I confirmed it by inspecting the stylesheet.',
               'Tested by looking at the code.')
        for text in bad:
            self.assertIn(evidence.READ_MSG, evidence.issues(self.ctx, text), text)
        ok = ('Mobile layout is not verified by reviewing the CSS alone; preview is unavailable.',
              "This hasn't been verified by reviewing anything yet.", 'I verified by running verify_sort.',
              'I reviewed the CSS. Mobile fit is not verified.')
        for text in ok:
            self.assertNotIn(evidence.READ_MSG, evidence.issues(self.ctx, text), text)
        self.assertFalse(evidence.issues(self.ctx, 'Checked by reading the HTML.', independent=True))
    def test_read_only_claim_never_reaches_text_stream(self):
        events=[]
        result=agent.run_turn([{'role':'user','content':'Make it fit mobile'}],self.reg,self.ctx,
            ('key','https://example.test/v1/chat/completions','test'),events.append,
            stream_fn=scripted([text_events('I verified by reviewing the CSS directly.') for _ in range(4)]),verify_request=True)
        self.assertEqual(result.status,'incomplete')
        self.assertNotIn('verified by reviewing', ''.join(e.get('delta','') for e in events))
    def test_receipts_reset_next_turn(self):
        self.write('names.csv','Surname\nÁlvarez\nReyes\n'); self.sort()
        agent.run_turn([{'role':'user','content':'Hello'}],self.reg,self.ctx,
            ('key','https://example.test','test'),lambda e:None,stream_fn=scripted([text_events('Hello')]))
        self.assertFalse(evidence.valid(self.ctx,'sort'))

if __name__ == '__main__': unittest.main()
