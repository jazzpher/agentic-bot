"""The HTTP stress harness, in the normal suite as one reduced run so it cannot rot.

stress_http.py is where the real load runs (the nightly Stress workflow). Its logic —
concurrent turns, the approval answers, Stop, parallel durable tasks, owner isolation
and the leak ceilings — is exercised here on two owners and one round, so a change to
the app or to the harness breaks a normal test run instead of waiting for the next
nightly.

One run, one test: the point is drift detection, and the harness is expensive enough
that running it twice would double the suite's slowest new step.
"""
import unittest

import stress_http

EXPECTED_CHECKS = ('concurrent /agent turns', 'one turn per session (409)',
                   'approval allow reaches the write', 'approval deny blocks the write',
                   'Stop closes an in-flight turn', 'parallel durable /task',
                   'owner isolation', 'no leaked threads or fds', 'no 5xx responses')


class StressHarnessTests(unittest.TestCase):
    def test_reduced_stress_run_passes_every_check(self):
        stress = stress_http.run(owners=2, rounds=1)
        failures = [row for row in stress.results if not row[1]]
        self.assertFalse(failures, '\n'.join('%s: %s' % (row[0], row[3]) for row in failures))
        names = [row[0] for row in stress.results]
        for expected in EXPECTED_CHECKS:
            self.assertIn(expected, names, 'a check stopped reporting instead of failing')


if __name__ == '__main__':
    unittest.main()
