import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import webui
from agent_loop import Workspace

class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(webui, 'client', return_value={'workspace': Workspace(self.root)})
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.c = webui.app.test_client()
    def get(self, name):
        return self.c.get('/file-preview', query_string={'name': name}, base_url='http://localhost')
    def test_bounded_text(self):
        (self.root/'large.txt').write_bytes(b'x'*70000)
        row=self.get('large.txt').json
        self.assertTrue(row['truncated']); self.assertEqual(len(row['text']),65536)
    def test_inert_unicode(self):
        (self.root/'a #.html').write_text('<script>test()</script> ₱',encoding='utf-8')
        self.assertEqual(self.get('a #.html').json['text'],'<script>test()</script> ₱')
    def test_traversal_and_missing(self):
        self.assertEqual(self.get('../outside').status_code,400)
        self.assertEqual(self.get('/etc/passwd').status_code,400)
        self.assertEqual(self.get('missing.txt').status_code,404)
    def test_binary(self):
        (self.root/'binary.pdf').write_bytes(b'%PDF\x00')
        self.assertEqual(self.get('binary.pdf').status_code,415)
    def test_utf8_boundary(self):
        (self.root/'boundary.txt').write_bytes(b'a'*65535+'₱'.encode())
        row=self.get('boundary.txt').json
        self.assertEqual(row['text'],'a'*65535); self.assertTrue(row['truncated'])
    def test_cross_owner(self):
        (self.root/'private.txt').write_text('private')
        with tempfile.TemporaryDirectory() as other, patch.object(webui,'client',return_value={'workspace':Workspace(other)}):
            self.assertEqual(self.get('private.txt').status_code,404)

    def test_symlink_escape_is_rejected(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / 'outside.txt'
            secret.write_text('outside workspace')
            (self.root / 'link.txt').symlink_to(secret)
            response = self.c.get('/file-preview?name=link.txt')
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('outside workspace', response.get_data(as_text=True))
