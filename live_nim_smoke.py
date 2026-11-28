"""Live NVIDIA NIM check: a real model, the real provider path, the real harness.

Every other test in this repository uses a scripted provider, so none of them
proves the end-to-end path works against an actual endpoint — docs/LIMITATIONS.md
says so ("real-model end-to-end runs are untested"). This does, when a key is
available:

    NVIDIA_API_KEY=nvapi-... python live_nim_smoke.py

Phase 1 goes through agenticbot.providers.stream_chat, the interactive agent's
path, and prints the model's own reply. Phase 2 submits a real durable task through
task_harness.TaskManager with a deterministic file_text check, so the model has to
calculate, write the file, read it back and pass the gate. Phase 3 forces the work
through run_command. Phase 4 is a debug cycle: failing tests, a fix, a re-run until
the suite is green. Phase 5 is the longest task the durable loop can run — four
independent bugs plus a file that does not exist yet, under the harness's own step
ceiling.

Phases 3-5 need AGENTIC_TASK_SHELL=on, and phase 4-5 need room, so raise the ceiling
with AGENTIC_TASK_MAX_ELAPSED (the 300 s default cuts a cycle that already finished).

Exit codes: 0 passed, 1 failed, 2 skipped (no key). The key is never printed.
"""
import json
import os
import pathlib
import sys
import tempfile
import time

import requests

from agent_loop import ACTION_SCHEMA
from agenticbot import providers
from task_harness import BUDGET_CEILINGS, TaskManager

NVIDIA_URL = 'https://integrate.api.nvidia.com/v1/chat/completions'
NVIDIA_MODEL = 'nvidia/nemotron-3-ultra-550b-a55b'
TASK_TIMEOUT = 420
GOAL = ('Compute 17 * 23 with the calculate action, write the result into sum.txt as '
        'its only content, read sum.txt back, then call done stating what you checked.')
SHELL_GOAL = ("Use the run_command action to create answer.txt containing exactly 391, for example: "
              'python3 -c "open(\'answer.txt\',\'w\').write(\'391\')". Then read answer.txt back with '
              'read_file and call done stating what you checked.')

# A real debug cycle: the module is wrong in two independent ways, so one command
# run cannot reveal everything and a single blind edit cannot pass the gate.
DEBUG_SOURCE = ('def average(values):\n'
                '    total = 0\n'
                '    for v in values:\n'
                '        total += v\n'
                '    return total / len(values) + 1\n')
DEBUG_COMMAND = 'python -m unittest -q test_calc'
# The correct module: the offset is gone and the empty list returns 0.
DEBUG_FIXED = ('def average(values):\n'
               '    if not values:\n'
               '        return 0\n'
               '    total = 0\n'
               '    for v in values:\n'
               '        total += v\n'
               '    return total / len(values)\n')
# A half fix: the offset is gone, but the empty list still divides by zero. The
# harness's own command check must reject it, which is what proves the gate bites.
DEBUG_HALF_FIXED = ('def average(values):\n'
                    '    total = 0\n'
                    '    for v in values:\n'
                    '        total += v\n'
                    '    return total / len(values)\n')
DEBUG_TESTS = ('import unittest\n'
               'from calc import average\n'
               '\n'
               'class AverageTests(unittest.TestCase):\n'
               '    def test_whole_numbers(self):\n'
               '        self.assertEqual(average([2, 4, 6]), 4)\n'
               '\n'
               '    def test_single(self):\n'
               '        self.assertEqual(average([10]), 10)\n'
               '\n'
               '    def test_empty_is_zero(self):\n'
               '        self.assertEqual(average([]), 0)\n'
               '\n'
               'if __name__ == "__main__":\n'
               '    unittest.main()\n')
DEBUG_GOAL = ('The tests in this workspace fail. Fix calc.py so that "python -m unittest -q test_calc" '
              'passes. Run the tests first to see the failures, then fix, then run them again. There is more '
              'than one bug. Do not edit test_calc.py: it is the specification and its content is a hard check. '
              'Report the failures you saw and what you changed.')
EXPECTED = '391'

# Phase 5: the longest task the durable loop can actually run. Four independent
# bugs in one module (empty input, an odd/even mistake, a wrong mode, a missing
# square root), plus a second file that does not exist yet and must produce an
# exact line. One reading of the failures cannot fix all of it, so the task has to
# sequence work instead of repeating one edit.
LONG_SOURCE = ('def mean(values):\n'
               '    return sum(values) / len(values)\n'
               '\n'
               'def median(values):\n'
               '    values = sorted(values)\n'
               '    return values[len(values) // 2]\n'
               '\n'
               'def mode(values):\n'
               '    return values[0]\n'
               '\n'
               'def stdev(values):\n'
               '    m = mean(values)\n'
               '    return sum((v - m) ** 2 for v in values) / len(values)\n')
LONG_TESTS = ('import unittest\n'
              'from stats import mean, median, mode, stdev\n'
              '\n'
              'class StatsTests(unittest.TestCase):\n'
              '    def test_mean(self):\n'
              '        self.assertEqual(mean([2, 4, 6]), 4)\n'
              '\n'
              '    def test_mean_empty_is_zero(self):\n'
              '        self.assertEqual(mean([]), 0)\n'
              '\n'
              '    def test_median_odd(self):\n'
              '        self.assertEqual(median([3, 1, 2]), 2)\n'
              '\n'
              '    def test_median_even(self):\n'
              '        self.assertEqual(median([4, 1, 3, 2]), 2.5)\n'
              '\n'
              '    def test_mode(self):\n'
              '        self.assertEqual(mode([1, 2, 2, 3]), 2)\n'
              '\n'
              '    def test_stdev(self):\n'
              '        self.assertAlmostEqual(stdev([2, 4, 4, 4, 5, 5, 7, 9]), 2.0)\n'
              '\n'
              'if __name__ == "__main__":\n'
              '    unittest.main()\n')
LONG_COMMAND = 'python -m unittest -q test_stats'
LONG_REPORT = 'python report.py'
LONG_OUTPUT = 'mean=5.00 median=4.50 mode=4 stdev=2.00'
# A correct solution and the report.py that produces LONG_OUTPUT. test_live_smoke.py
# drives this phase with them offline, and they document what "done" means here.
LONG_FIXED = ('def mean(values):\n'
              '    return sum(values) / len(values) if values else 0\n'
              '\n'
              'def median(values):\n'
              '    values = sorted(values)\n'
              '    middle = len(values) // 2\n'
              '    if len(values) % 2:\n'
              '        return values[middle]\n'
              '    return (values[middle - 1] + values[middle]) / 2\n'
              '\n'
              'def mode(values):\n'
              '    return max(sorted(set(values)), key=values.count)\n'
              '\n'
              'def stdev(values):\n'
              '    m = mean(values)\n'
              '    return (sum((v - m) ** 2 for v in values) / len(values)) ** 0.5\n')
LONG_REPORT_SOURCE = ('from stats import mean, median, mode, stdev\n'
                      'data = [2, 4, 4, 4, 5, 5, 7, 9]\n'
                      'print("mean=%.2f median=%.2f mode=%d stdev=%.2f"\n'
                      '      % (mean(data), median(data), mode(data), stdev(data)))\n')
LONG_GOAL = ('The tests in this workspace fail. Fix stats.py so that "python -m unittest -q test_stats" passes '
             '- there is more than one bug. Then create report.py: running "python report.py" must print exactly '
             'this one line, for the data [2, 4, 4, 4, 5, 5, 7, 9] (use %.2f for the floats and %d for the mode):\n'
             + LONG_OUTPUT + '\n'
             'Build report.py on the functions in stats.py. Do not edit test_stats.py: it is the specification and '
             'its content is a hard check. Run the tests before and after your fix, then run report.py, and report '
             'what you changed.')
TRANSCRIPT = ('action', 'tool', 'check', 'verification', 'done', 'result', 'error', 'blocked', 'retry')


def provider_outage(events):
    """Classify a blocked task as a provider outage, with the reason and retries.

    The durable loop pauses on a provider error with a friendly message ("Can't
    reach The AI provider right now"); the nightly log used to show only the
    status and the failing gates, so a quota/endpoint problem looked like a
    broken agent. A retry event names the same provider message, so the count
    here is what the harness actually tried before it paused.
    """
    blocked = [str(e.get('text', '')) for e in events if e.get('type') == 'blocked']
    if not blocked:
        return None
    text = blocked[-1]
    if not any(token in text for token in ('provider', 'AI provider', 'quota', 'reach', 'key', 'model')):
        return None
    retries = sum(1 for e in events if e.get('type') == 'retry' and 'retrying' in str(e.get('text', '')))
    return '%s (%d automatic %s)' % (text, retries, 'retry' if retries == 1 else 'retries')


def config():
    key = (os.environ.get('NVIDIA_API_KEY') or os.environ.get('AGENTIC_API_KEY') or '').strip()
    url = (os.environ.get('AGENTIC_API_URL') or NVIDIA_URL).strip()
    model = (os.environ.get('AGENTIC_MODEL') or NVIDIA_MODEL).strip()
    return key, url, model


def is_nim_nemotron(cfg):
    from urllib.parse import urlparse
    host = (urlparse(cfg[1]).hostname or '').lower()
    return host == 'integrate.api.nvidia.com' and 'nemotron' in (cfg[2] or '').lower()


def think_off_extra(cfg):
    """Nemotron spends a small structured-output budget thinking and returns content=null.

    agenticbot/grader.py already opts out for exactly this host and model family; the
    persistent output does not change the wire shape, only this vendor switch.
    """
    return {'chat_template_kwargs': {'enable_thinking': False}} if is_nim_nemotron(cfg) else None


def phase_chat(cfg):
    """The interactive agent's streaming path: does the key work and does the model answer?"""
    text = []
    usage = None
    for event in providers.stream_chat(cfg, [{'role': 'user', 'content': 'Reply with exactly: NIM OK'}],
                                       extra=think_off_extra(cfg), total_timeout=120):
        if event.get('type') == 'text':
            text.append(event.get('delta') or '')
        elif event.get('type') == 'usage':
            usage = event
    return ' '.join(''.join(text).split()), usage


def task_call(cfg):
    """Same request shape as webui.task_provider: one structured JSON action per reply."""
    def call(messages, max_output_bytes=4000):
        call.last_usage = None
        limit = max_output_bytes * 6 + 65536
        url = cfg[1]
        payload = {'model': cfg[2], 'messages': messages, 'stream': False,
                   'response_format': {'type': 'json_schema',
                                       'json_schema': {'name': 'task_action', 'strict': True,
                                                       'schema': ACTION_SCHEMA}}}
        extra = think_off_extra(cfg)
        if extra:
            payload.update(extra)
        headers = {'Authorization': 'Bearer ' + cfg[0], 'Content-Type': 'application/json'}

        def post(body):
            return requests.post(url, headers=headers, json=body, timeout=(10, 150))

        res = post(payload)
        if res.status_code == 400:
            body = res.text[:2000]
            res.close()
            if any(token in body.lower() for token in ('response_format', 'json_schema', 'json schema')):
                print('  provider refused response_format; retrying with plain JSON mode')
                payload.pop('response_format', None)
                res = post(payload)
            else:
                raise ValueError('HTTP 400: ' + ' '.join(body.split())[:300])
        try:
            res.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', '?')
            res.close()
            raise ValueError('HTTP %s from the provider.' % status) from exc
        data = res.json()
        usage = data.get('usage')
        if isinstance(usage, dict):
            prompt, completion = usage.get('prompt_tokens'), usage.get('completion_tokens')
            if type(prompt) is int and type(completion) is int:
                call.last_usage = {'prompt_tokens': prompt, 'completion_tokens': completion}
        try:
            choice = data['choices'][0]
        except (KeyError, IndexError, TypeError):
            raise ValueError('Provider reply had no choices: ' + json.dumps(data)[:300]) from None
        if choice.get('finish_reason') not in (None, 'stop'):
            raise ValueError('Reply was filtered, truncated or incomplete (finish_reason=%s).'
                             % choice.get('finish_reason'))
        content = (choice.get('message') or {}).get('content')
        if isinstance(content, list):
            content = ''.join(part.get('text', '') for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise ValueError('Provider returned empty content; the model spent its output budget '
                             'before answering (enable_thinking is the usual cause on Nemotron).')
        if len(content.encode('utf-8')) > max_output_bytes:
            raise ValueError('Provider reply exceeded the output budget.')
        return content
    call.last_usage = None
    return call


def wait_for(m, tid, timeout=TASK_TIMEOUT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = m.get(tid, 'nim')
        if row and row['status'] not in ('queued', 'running'):
            return row
        time.sleep(.2)
    raise RuntimeError('task did not settle within %ds' % timeout)


def phase_task(cfg, root):
    """A real durable task: the model must produce 391, write it, read it back and pass the check."""
    m = TaskManager(root / 'tasks.db', root / 'workspaces')
    started = time.monotonic()
    try:
        tid = m.submit('nim', GOAL, [], task_call(cfg),
                       checks=[{'type': 'file_text', 'path': 'sum.txt', 'text': EXPECTED}],
                       budgets={'steps': 8, 'elapsed': 300})
        row = wait_for(m, tid)
        elapsed = time.monotonic() - started
        for event in m.events(tid, 'nim'):
            kind = event.get('type')
            if kind not in TRANSCRIPT:
                continue
            text = ' '.join(str(event.get('text', '')).split())
            print('    %-12s %s' % (kind, text[:240]))
        artifact = m.workspaces / 'nim' / 'sum.txt'
        got = artifact.read_text(encoding='utf-8', errors='replace') if artifact.is_file() else None
        return row, elapsed, got
    finally:
        try:
            m.close()
        except Exception:  # noqa: BLE001 - teardown must not hide the result
            pass


def phase_shell(cfg, root):
    """The same durable task, but only a shell command can produce the artifact.

    Phase 2 proves the file tools; this proves run_command reaches the sandbox
    backend, is recorded as an action, and still ends in a checked completion.
    """
    m = TaskManager(root / 'tasks.db', root / 'workspaces')
    started = time.monotonic()
    try:
        tid = m.submit('nim', SHELL_GOAL, [], task_call(cfg),
                       checks=[{'type': 'file_text', 'path': 'answer.txt', 'text': EXPECTED}],
                       budgets={'steps': 8, 'elapsed': 300})
        row = wait_for(m, tid)
        elapsed = time.monotonic() - started
        events = m.events(tid, 'nim')
        for event in events:
            kind = event.get('type')
            if kind not in TRANSCRIPT:
                continue
            text = ' '.join(str(event.get('text', '')).split())
            print('    %-12s %s' % (kind, text[:240]))
        commands = [str(e.get('arg', ''))[:120] for e in events
                    if e.get('type') == 'action' and 'run_command' in str(e.get('text', ''))]
        artifact = m.workspaces / 'nim' / 'answer.txt'
        got = artifact.read_text(encoding='utf-8', errors='replace') if artifact.is_file() else None
        return row, elapsed, got, commands
    finally:
        try:
            m.close()
        except Exception:  # noqa: BLE001 - teardown must not hide the result
            pass


def phase_debug(cfg, root, elapsed_budget=300, call_model=None):
    """A real debug cycle: failing tests, a shell, and a gate the harness runs itself.

    The workspace starts broken in two ways, so the model has to run the tests,
    read the failures, fix one thing, run again, fix the rest, and run until the
    suite is green. The gate is a command check the harness executes with the
    sandbox backend, plus a file_text check pinning the test file so a rewritten
    specification cannot pass.

    ``call_model`` overrides the provider, which is how test_live_smoke.py drives
    this end to end in CI without a key.
    """
    m = TaskManager(root / 'tasks.db', root / 'workspaces')
    ws = m.workspaces / 'nim'
    ws.mkdir(parents=True, exist_ok=True)
    (ws / 'calc.py').write_text(DEBUG_SOURCE, encoding='utf-8')
    (ws / 'test_calc.py').write_text(DEBUG_TESTS, encoding='utf-8')
    started = time.monotonic()
    try:
        tid = m.submit('nim', DEBUG_GOAL, [], call_model or task_call(cfg),
                       checks=[{'type': 'command', 'command': DEBUG_COMMAND,
                                'expect_exit': 0, 'timeout': 60},
                               {'type': 'file_text', 'path': 'test_calc.py', 'text': DEBUG_TESTS}],
                       budgets={'steps': 12, 'elapsed': elapsed_budget})
        row = wait_for(m, tid, timeout=elapsed_budget + 120)
        elapsed = time.monotonic() - started
        events = m.events(tid, 'nim')
        for event in events:
            kind = event.get('type')
            if kind not in TRANSCRIPT:
                continue
            text = ' '.join(str(event.get('text', '')).split())
            print('    %-12s %s' % (kind, text[:240]))
        commands = [str(e.get('arg', ''))[:120] for e in events
                    if e.get('type') == 'action' and 'run_command' in str(e.get('text', ''))]
        fixed = (ws / 'calc.py').read_text(encoding='utf-8', errors='replace')
        # The decisive check, run here rather than trusted from the task status:
        # does the artifact actually pass the acceptance command now?
        from agenticbot import sandbox
        gate = sandbox.get_backend().run(DEBUG_COMMAND, ws, 60, network=False)
        return row, elapsed, fixed, commands, gate
    finally:
        try:
            m.close()
        except Exception:  # noqa: BLE001 - teardown must not hide the result
            pass


def phase_long(cfg, root, elapsed_budget=300, call_model=None):
    """A long-horizon task: several bugs, a file that must be created, an exact output.

    Three gates, all run by the harness and none by the model: the pinned test file
    (so the specification cannot be rewritten), the test command, and a command
    whose exact output the new report.py has to produce. The step budget is the
    harness's own ceiling, which is what makes this the longest task it can run.

    ``call_model`` overrides the provider, which is how a scripted model can drive
    the same task offline.
    """
    m = TaskManager(root / 'tasks.db', root / 'workspaces')
    ws = m.workspaces / 'nim'
    ws.mkdir(parents=True, exist_ok=True)
    (ws / 'stats.py').write_text(LONG_SOURCE, encoding='utf-8')
    (ws / 'test_stats.py').write_text(LONG_TESTS, encoding='utf-8')
    started = time.monotonic()
    try:
        tid = m.submit('nim', LONG_GOAL, [], call_model or task_call(cfg),
                       checks=[{'type': 'command', 'command': LONG_COMMAND, 'expect_exit': 0, 'timeout': 60},
                               {'type': 'file_text', 'path': 'test_stats.py', 'text': LONG_TESTS},
                               {'type': 'command', 'command': LONG_REPORT, 'expect_output': LONG_OUTPUT,
                                'timeout': 60}],
                       budgets={'steps': BUDGET_CEILINGS['steps'], 'elapsed': elapsed_budget})
        row = wait_for(m, tid, timeout=elapsed_budget + 180)
        elapsed = time.monotonic() - started
        events = m.events(tid, 'nim')
        for event in events:
            kind = event.get('type')
            if kind not in TRANSCRIPT:
                continue
            text = ' '.join(str(event.get('text', '')).split())
            print('    %-12s %s' % (kind, text[:200]))
        outage = provider_outage(events)
        if outage:
            # The failure stays a failure, but the nightly log now says whether
            # the provider paused the task or the agent actually ran out of steps.
            print('  PROVIDER OUTAGE: ' + outage)
        commands = [str(e.get('arg', ''))[:120] for e in events
                    if e.get('type') == 'action' and 'run_command' in str(e.get('text', ''))]
        from agenticbot import sandbox
        backend = sandbox.get_backend()
        tests_gate = backend.run(LONG_COMMAND, ws, 60, network=False)
        report_gate = backend.run(LONG_REPORT, ws, 60, network=False)
        return row, elapsed, commands, tests_gate, report_gate
    finally:
        try:
            m.close()
        except Exception:  # noqa: BLE001 - teardown must not hide the result
            pass


def main():
    cfg = config()
    print('NVIDIA NIM live check -> %s' % cfg[1])
    print('model: %s' % cfg[2])
    if not cfg[0]:
        print('SKIP: no key. Set NVIDIA_API_KEY (or AGENTIC_API_KEY) and run again.')
        return 2
    print('key: %s...(%d characters, not printed)' % (cfg[0][:6], len(cfg[0])))
    failures = []

    print('\nphase 1: streaming chat through providers.stream_chat')
    try:
        reply, usage = phase_chat(cfg)
        print('  reply: %r' % reply[:120])
        if usage:
            print('  tokens: prompt=%s completion=%s'
                  % (usage.get('prompt_tokens'), usage.get('completion_tokens')))
        if not reply:
            raise RuntimeError('empty reply')
    except Exception as exc:  # noqa: BLE001 - the smoke reports, it never crashes
        print('  FAILED: %s: %s' % (type(exc).__name__, exc))
        failures.append('streaming chat')

    print('\nphase 2: durable task through TaskManager, checked with file_text')
    with tempfile.TemporaryDirectory() as tmp:
        try:
            row, elapsed, got = phase_task(cfg, pathlib.Path(tmp))
            state = row['state']
            print('  status: %s in %.1fs (%d step(s), %d output bytes)'
                  % (row['status'], elapsed, state.get('steps', 0), state.get('output', 0)))
            if state.get('usage'):
                print('  tokens: prompt=%s completion=%s'
                      % (state['usage'].get('prompt_tokens'), state['usage'].get('completion_tokens')))
            print('  sum.txt: %r' % (got,))
            if row['status'] != 'completed':
                failures.append('durable task (%s)' % row['status'])
        except Exception as exc:  # noqa: BLE001
            print('  FAILED: %s: %s' % (type(exc).__name__, exc))
            failures.append('durable task')

    from agenticbot import config as gconfig
    if not gconfig.TASK_SHELL:
        print('\nphase 3: SKIPPED (AGENTIC_TASK_SHELL is off; the durable loop has no shell)')
    else:
        print('\nphase 3: durable task that can only finish by running a shell command')
        with tempfile.TemporaryDirectory() as tmp:
            try:
                row, elapsed, got, commands = phase_shell(cfg, pathlib.Path(tmp))
                state = row['state']
                print('  status: %s in %.1fs (%d step(s))'
                      % (row['status'], elapsed, state.get('steps', 0)))
                print('  run_command calls: %d %s' % (len(commands), commands))
                print('  answer.txt: %r' % (got,))
                if row['status'] != 'completed':
                    failures.append('shell task (%s)' % row['status'])
                elif not commands:
                    print('  NOTE: completed without run_command; the model reached the file another way.')
            except Exception as exc:  # noqa: BLE001
                print('  FAILED: %s: %s' % (type(exc).__name__, exc))
                failures.append('shell task')

        print('\nphase 4: real debug cycle - failing tests, fix, re-run until green')
        with tempfile.TemporaryDirectory() as tmp:
            try:
                # A debug cycle needs room: the 300 s default cut a run that had
                # already fixed both bugs. The operator raises the ceiling with
                # AGENTIC_TASK_MAX_ELAPSED; never ask for more than it allows.
                budget = min(600, BUDGET_CEILINGS['elapsed'])
                print('  elapsed budget: %ss (ceiling %ss; raise it with AGENTIC_TASK_MAX_ELAPSED)'
                      % (budget, BUDGET_CEILINGS['elapsed']))
                row, elapsed, fixed, commands, gate = phase_debug(cfg, pathlib.Path(tmp),
                                                                 elapsed_budget=budget)
                state = row['state']
                print('  status: %s in %.1fs (%d step(s))'
                      % (row['status'], elapsed, state.get('steps', 0)))
                print('  run_command calls: %d' % len(commands))
                for command in commands:
                    print('    $ %s' % command)
                print('  final calc.py:')
                for line in fixed.splitlines() or ['(missing)']:
                    print('    | ' + line)
                print('  acceptance command now: exit %s' % gate.exit_code)
                if gate.render().strip():
                    for line in gate.render().strip().splitlines()[-6:]:
                        print('    > ' + line)
                if row['status'] != 'completed':
                    failures.append('debug cycle (%s)' % row['status'])
                elif len(commands) < 2:
                    print('  NOTE: completed with fewer than two test runs; the cycle was not exercised.')
            except Exception as exc:  # noqa: BLE001
                print('  FAILED: %s: %s' % (type(exc).__name__, exc))
                failures.append('debug cycle')

        print('\nphase 5: long-horizon task - four bugs, a new file, an exact output')
        print('  step ceiling: %d (the harness hard-caps steps; only elapsed is operator-raisable)'
              % BUDGET_CEILINGS['steps'])
        with tempfile.TemporaryDirectory() as tmp:
            try:
                budget = min(600, BUDGET_CEILINGS['elapsed'])
                row, elapsed, commands, tests_gate, report_gate = phase_long(
                    cfg, pathlib.Path(tmp), elapsed_budget=budget)
                state = row['state']
                used = state.get('steps', 0)
                print('  status: %s in %.1fs (%d of %d step(s) used)'
                      % (row['status'], elapsed, used, BUDGET_CEILINGS['steps']))
                if state.get('usage'):
                    print('  tokens: prompt=%s completion=%s'
                          % (state['usage'].get('prompt_tokens'), state['usage'].get('completion_tokens')))
                print('  run_command calls: %d' % len(commands))
                for command in commands:
                    print('    $ %s' % command)
                print('  tests gate now: exit %s' % tests_gate.exit_code)
                print('  report gate now: exit %s' % report_gate.exit_code)
                for line in report_gate.render().strip().splitlines()[-4:]:
                    print('    > ' + line)
                if row['status'] != 'completed':
                    failures.append('long-horizon task (%s after %d steps)' % (row['status'], used))
                elif tests_gate.exit_code != 0 or report_gate.exit_code != 0:
                    # The task said completed but the gates disagree: that is a false
                    # completion, which is worse than a budget stop.
                    failures.append('long-horizon task claimed done but the gates fail')
                if used >= BUDGET_CEILINGS['steps']:
                    print('  NOTE: it used every step of the ceiling; anything longer cannot run today.')
            except Exception as exc:  # noqa: BLE001
                print('  FAILED: %s: %s' % (type(exc).__name__, exc))
                failures.append('long-horizon task')

    print()
    if failures:
        print('FAILED: ' + ', '.join(failures))
        if is_nim_nemotron(cfg):
            print('Hint: this key and model are NVIDIA Nemotron; the grader disables thinking for it '
                  'because a small structured-output budget otherwise returns empty content.')
        return 1
    print('PASSED: the key works and a real model completed a checked task through the harness.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
