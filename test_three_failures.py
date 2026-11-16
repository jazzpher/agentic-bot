"""Regressions for three failures seen in live debug-cycle runs.

1. A correct fix that ran out of time was marked budget_exhausted. The harness must
   re-run the owner's acceptance checks before giving up on a budget, and complete
   only when they pass.
2. A model resent the same rejected done forever. Repeats must stop with a reason.
3. A goal said ALPHA while the check wanted BETA, and the model switched silently.
   The conflict must be surfaced to the model and repeated on the result.

Every negative case proves the gate still bites: a half fix, a claim with nothing to
show, and a Stop must never complete.
"""
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from agenticbot import config
from task_harness import TaskManager, contract_conflicts, goal_literals


def action(kind, **kw):
    return json.dumps({'action': kind, **kw})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        on = mock.patch.object(config, 'TASK_SHELL', True)
        on.start()
        self.addCleanup(on.stop)

    def manager(self):
        m = TaskManager(self.root / 'tasks.db', self.root / 'workspaces')
        self.addCleanup(m.close)
        return m

    def wait(self, m, tid, owner='alice', timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            row = m.get(tid, owner)
            if row['status'] not in ('queued', 'running'):
                return row
            time.sleep(.01)
        self.fail('worker did not settle')

    def ws(self, owner='alice'):
        path = self.root / 'workspaces' / owner
        path.mkdir(parents=True, exist_ok=True)
        return path


class BudgetRerunTests(Base):
    def test_a_correct_fix_is_completed_not_lost_at_the_time_cap(self):
        # The model writes the right file and reads it back, then the budget runs out
        # before it says done. The acceptance check is already green, so the task
        # must complete on the harness's own re-run instead of being discarded.
        m = self.manager()
        ws = self.ws()
        calls = []

        def provider(messages, max_output_bytes=4000):
            calls.append(1)
            if len(calls) == 1:
                return action('write_file', path='answer.txt', content='391')
            if len(calls) == 2:
                return action('read_file', path='answer.txt')
            # Burn the elapsed budget here, after the correct artifact exists.
            time.sleep(0.6)
            return action('plan', text='still thinking')

        tid = m.submit('alice', 'Write 391 to answer.txt', [], provider,
                       checks=[{'type': 'file_text', 'path': 'answer.txt', 'text': '391'}],
                       budgets={'steps': 10, 'elapsed': 0.5})
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed', [e['text'] for e in m.events(tid, 'alice')][-3:])
        self.assertTrue(any(e['type'] == 'result' and 'Explicit artifact checks passed' in e['text']
                            for e in m.events(tid, 'alice')))
        self.assertEqual((ws / 'answer.txt').read_text(), '391')

    def test_a_written_but_unread_artifact_still_completes_at_the_cap(self):
        # The budget boundary cannot ask the model to read its own output back, so
        # the owner's check decides. Otherwise a finished artifact is discarded for
        # a read-back nobody can request any more.
        m = self.manager()
        calls = []

        def provider(messages, max_output_bytes=4000):
            calls.append(1)
            if len(calls) == 1:
                return action('write_file', path='answer.txt', content='391')
            time.sleep(0.6)
            return action('plan', text='still thinking')

        tid = m.submit('alice', 'Write 391 to answer.txt', [], provider,
                       checks=[{'type': 'file_text', 'path': 'answer.txt', 'text': '391'}],
                       budgets={'steps': 10, 'elapsed': 0.5})
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed', [e['text'] for e in m.events(tid, 'alice')][-3:])

    def test_a_wrong_artifact_at_the_time_cap_still_exhausts(self):
        # The re-run is a real check: a half-fixed artifact must not complete just
        # because the clock ran out.
        m = self.manager()
        calls = []

        def provider(messages, max_output_bytes=4000):
            calls.append(1)
            if len(calls) == 1:
                return action('write_file', path='answer.txt', content='390')
            time.sleep(0.6)
            return action('plan', text='still thinking')

        tid = m.submit('alice', 'Write 391 to answer.txt', [], provider,
                       checks=[{'type': 'file_text', 'path': 'answer.txt', 'text': '391'}],
                       budgets={'steps': 10, 'elapsed': 0.5})
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'budget_exhausted')

    def test_a_budget_stop_never_completes_on_a_model_claim_alone(self):
        # No checks: a model claim at the budget boundary is not evidence.
        m = self.manager()

        def provider(messages, max_output_bytes=4000):
            time.sleep(0.6)
            return action('done', text='all done, trust me')

        tid = m.submit('alice', 'Say hello', [], provider, budgets={'steps': 4, 'elapsed': 0.3})
        row = self.wait(m, tid)
        self.assertNotEqual(row['status'], 'completed')

    def test_budget_ceiling_is_raised_only_by_the_operator(self):
        import task_harness
        self.assertEqual(task_harness.DEFAULT_BUDGETS['elapsed'], 300)
        self.assertGreaterEqual(task_harness.BUDGET_CEILINGS['elapsed'], 300)
        m = self.manager()
        with self.assertRaises(ValueError):
            m.submit('alice', 'x', [], lambda _: action('done', text='x'),
                     budgets={'elapsed': task_harness.BUDGET_CEILINGS['elapsed'] + 1})


class RepeatedClaimTests(Base):
    def test_the_same_rejected_claim_stops_with_a_reason(self):
        m = self.manager()
        calls = []

        def provider(messages, max_output_bytes=4000):
            calls.append(1)
            return action('done', text='re-ran it green')

        tid = m.submit('alice', 'Make the tests pass', [], provider,
                       checks=[{'type': 'file_text', 'path': 'answer.txt', 'text': '391'}],
                       budgets={'steps': 12})
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'blocked')
        self.assertLess(len(calls), 6, 'an identical claim must not burn the whole step budget')
        reason = [e['text'] for e in m.events(tid, 'alice') if e['type'] == 'blocked'][-1]
        self.assertIn('not accepted', reason)
        self.assertIn('nothing in the workspace changed', reason)

    def test_a_real_change_between_claims_resets_the_stall_count(self):
        m = self.manager()
        replies = iter([action('done', text='re-ran it green'),
                        action('write_file', path='scratch.txt', content='x'),
                        action('read_file', path='scratch.txt'),
                        action('done', text='re-ran it green'),
                        action('done', text='re-ran it green'),
                        action('write_file', path='answer.txt', content='391'),
                        action('read_file', path='answer.txt'),
                        action('done', text='answer written and read back')])
        tid = m.submit('alice', 'Write 391 to answer.txt', [], lambda _: next(replies),
                       checks=[{'type': 'file_text', 'path': 'answer.txt', 'text': '391'}],
                       budgets={'steps': 12})
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed', [e['text'] for e in m.events(tid, 'alice')][-2:])


class ConflictTests(Base):
    def test_goal_literals_are_quoted_or_all_caps_values(self):
        self.assertEqual(goal_literals('Write "ALPHA" to out.txt'), ['ALPHA'])
        self.assertEqual(goal_literals("Write ALPHA to out.txt, then say OK"), ['ALPHA', 'OK'])
        self.assertEqual(goal_literals('write the greeting in lowercase'), [])

    def test_a_goal_alpha_against_a_beta_check_is_flagged(self):
        notes = contract_conflicts('Write ALPHA to out.txt',
                                   [{'type': 'file_text', 'path': 'out.txt', 'text': 'BETA'}])
        self.assertEqual(len(notes), 1)
        self.assertIn('ALPHA', notes[0])
        self.assertIn('BETA', notes[0])

    def test_agreeing_goal_and_check_are_not_flagged(self):
        self.assertEqual(contract_conflicts('Write ALPHA to out.txt',
                                            [{'type': 'file_text', 'path': 'out.txt', 'text': 'ALPHA'}]), [])
        self.assertEqual(contract_conflicts('Write hello to out.txt',
                                            [{'type': 'file_text', 'path': 'out.txt', 'text': 'hello'}]), [])

    def test_a_command_checks_expected_output_is_covered_too(self):
        notes = contract_conflicts('Make the tool print ALPHA',
                                   [{'type': 'command', 'command': 'python tool.py', 'expect_output': 'BETA'}])
        self.assertEqual(len(notes), 1)
        self.assertIn('ALPHA', notes[0])
        self.assertIn('BETA', notes[0])

    def test_a_document_expectation_is_not_a_named_value(self):
        # The goal mentioning API/JSON is not a disagreement with a check that
        # expects a document: only word-shaped values on both sides can clash.
        self.assertEqual(contract_conflicts('Build a REST API returning JSON',
                                            [{'type': 'file_text', 'path': 'out.json',
                                              'text': '{"ok": true}'}]), [])
        self.assertEqual(contract_conflicts('Write ALPHA to out.txt',
                                            [{'type': 'file_text', 'path': 'out.txt',
                                              'text': '{"ok": true}'}]), [])

    def test_a_value_that_contains_the_other_is_not_a_conflict(self):
        self.assertEqual(contract_conflicts('Write ALPHA to out.txt',
                                            [{'type': 'file_text', 'path': 'out.txt',
                                              'text': 'ALPHA-v2'}]), [])

    def test_the_container_ships_a_raised_elapsed_ceiling(self):
        # The code fix is worthless if the deployed image keeps the 300 s default:
        # a live debug cycle took 159-329 s, so the ceiling is what decides.
        import pathlib
        dockerfile = pathlib.Path('Dockerfile').read_text()
        self.assertIn('AGENTIC_TASK_MAX_ELAPSED', dockerfile)
        self.assertIn('AGENTIC_TASK_SHELL=on', dockerfile)

    def test_the_conflict_is_recorded_and_put_in_front_of_the_model(self):
        m = self.manager()
        seen = []

        def provider(messages, max_output_bytes=4000):
            seen.append(json.dumps(messages, ensure_ascii=False))
            return action('need_input', text='ALPHA or BETA?')

        tid = m.submit('alice', 'Write ALPHA to out.txt', [], provider,
                       checks=[{'type': 'file_text', 'path': 'out.txt', 'text': 'BETA'}])
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'needs_input')
        self.assertTrue(row['state']['contract_conflicts'])
        self.assertTrue(any(e['type'] == 'context' and 'Possible conflict' in e['text']
                            for e in m.events(tid, 'alice')))
        self.assertIn('Never silently pick one', seen[0])

    def test_a_completed_task_repeats_the_conflict_on_its_result(self):
        m = self.manager()
        replies = iter([action('write_file', path='out.txt', content='BETA'),
                        action('read_file', path='out.txt'),
                        action('done', text='wrote BETA, read it back')])
        tid = m.submit('alice', 'Write ALPHA to out.txt', [], lambda _: next(replies),
                       checks=[{'type': 'file_text', 'path': 'out.txt', 'text': 'BETA'}])
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed')
        result = [e['text'] for e in m.events(tid, 'alice') if e['type'] == 'result'][-1]
        self.assertIn('Noted conflict', result)
        self.assertIn('ALPHA', result)


if __name__ == '__main__':
    unittest.main()
