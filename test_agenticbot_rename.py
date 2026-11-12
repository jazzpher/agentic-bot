import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from agenticbot.env import env_get

class RenameTests(unittest.TestCase):
    def test_environment_fallback_and_new_precedence(self):
        with patch.dict(os.environ, {'GROK_MODEL':'old','GROKBOT_EXEC_DISABLED':'1'}, clear=True):
            self.assertEqual(env_get('AGENTIC_MODEL'), 'old')
            self.assertEqual(env_get('AGENTICBOT_EXEC_DISABLED'), '1')
            self.assertEqual(env_get('AGENTIC_MISSING','fallback'), 'fallback')
            os.environ['AGENTIC_MODEL']='new'
            self.assertEqual(env_get('AGENTIC_MODEL'), 'new')
            os.environ['AGENTIC_MODEL']=''
            self.assertEqual(env_get('AGENTIC_MODEL'), '')
            self.assertEqual(os.environ['GROK_MODEL'],'old')
            os.environ['OPENAI_API_KEY']='provider-value'
            self.assertEqual(env_get('OPENAI_API_KEY'), 'provider-value')
            self.assertEqual(env_get('XDG_DATA_HOME','default'), 'default')
    def test_old_imports_are_same_modules(self):
        for name in ['config','agent','cli','tools','tools.files','approvals','gateway.telegram']:
            with self.subTest(name=name):
                self.assertIs(importlib.import_module('grokbot.'+name), importlib.import_module('agenticbot.'+name))
    def test_module_commands(self):
        for module in ['agenticbot','grokbot']:
            result=subprocess.run([sys.executable,'-m',module,'--version'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('Agentic Bot 0.9.3',result.stdout)
    def test_old_data_is_reused_and_new_installs_use_new_name(self):
        from agenticbot import backup
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'XDG_DATA_HOME':d,'AGENTIC_WORKSPACE_DIR':'','GROK_WORKSPACE_DIR':''},clear=False):
            self.assertEqual(backup.default_data_dir(),Path(d)/'agenticbot'/'workspaces')
            old=Path(d)/'grokbot'/'workspaces';old.mkdir(parents=True)
            self.assertEqual(backup.default_data_dir(),old)
            os.environ['GROK_WORKSPACE_DIR']=d+'/custom'
            os.environ.pop('AGENTIC_WORKSPACE_DIR')
            self.assertEqual(backup.default_data_dir(),Path(d)/'custom')
    def test_legacy_project_instructions(self):
        from agenticbot.agent import PROJECT_INSTRUCTION_FILES
        self.assertIn('AGENTICBOT.md',PROJECT_INSTRUCTION_FILES)
        self.assertIn('GROKBOT.md',PROJECT_INSTRUCTION_FILES)
    def test_ui_brand_and_static_assets(self):
        import webui
        client=webui.app.test_client()
        with patch.object(webui,'PASSWORD',''):
            response=client.get('/')
            self.assertEqual(response.status_code,200)
            html=response.get_data(as_text=True)
            self.assertIn('Agentic Bot',html)
            self.assertNotIn('>grok-bot<',html)
            for asset in ['/static/app.js','/static/app.css']:
                self.assertEqual(client.get(asset).status_code,200,asset)
    def test_backup_format_stays_compatible(self):
        from agenticbot import backup
        self.assertEqual(backup.MANIFEST,'grokbot-backup.json')

if __name__=='__main__': unittest.main()
