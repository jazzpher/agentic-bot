"""Subprocess checks: real watchdog behavior, without risking the test runner."""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

RUNNER = pathlib.Path(__file__).with_name('ci_unittest.py')


class WatchdogTests(unittest.TestCase):
    def run_case(self, body, seconds='2'):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, 'probe.py').write_text('import unittest, time\nclass Probe(unittest.TestCase):\n    def test_probe(self):\n        ' + body + '\n')
            return subprocess.run([sys.executable, str(RUNNER), '--timeout', seconds, 'probe', '-v'],
                                  cwd=tmp, env={**os.environ, 'PYTHONPATH': tmp},
                                  capture_output=True, text=True, timeout=15)

    def test_pass_exits_zero(self):
        self.assertEqual(self.run_case('pass').returncode, 0)

    def test_failure_exits_nonzero(self):
        self.assertEqual(self.run_case('self.fail("expected")').returncode, 1)

    def test_hang_exits_124_and_names_test(self):
        result = self.run_case('time.sleep(30)', '0.2')
        self.assertEqual(result.returncode, 124)
        self.assertIn('TIMEOUT', result.stderr)
        self.assertIn('test_probe', result.stderr)

    def test_timeout_includes_cleanup(self):
        result = self.run_case('self.addCleanup(time.sleep, 30)', '0.2')
        self.assertEqual(result.returncode, 124)
