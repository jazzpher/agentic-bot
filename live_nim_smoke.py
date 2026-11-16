"""Live NVIDIA NIM check: a real model, the real provider path, the real harness.

Every other test in this repository uses a scripted provider, so none of them
proves the end-to-end path works against an actual endpoint — docs/LIMITATIONS.md
says so ("real-model end-to-end runs are untested"). This does, when a key is
available:

    NVIDIA_API_KEY=nvapi-... python live_nim_smoke.py

Phase 1 goes through agenticbot.providers.stream_chat, the interactive agent's
path, and prints the model's own reply. Phase 2 submits a real durable task through
task_harness.TaskManager with a deterministic file_text check, so the model has to
calculate, write the file, read it back and pass the gate.

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
from task_harness import TaskManager

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
TRANSCRIPT = ('action', 'tool', 'check', 'verification', 'done', 'result', 'error', 'blocked')


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


def phase_debug(cfg, root, elapsed_budget=300):
    """A real debug cycle: failing tests, a shell, and a gate the harness runs itself.

    The workspace starts broken in two ways, so the model has to run the tests,
    read the failures, fix one thing, run again, fix the rest, and run until the
    suite is green. The gate is a command check the harness executes with the
    sandbox backend, plus a file_text check pinning the test file so a rewritten
    specification cannot pass.
    """
    m = TaskManager(root / 'tasks.db', root / 'workspaces')
    ws = m.workspaces / 'nim'
    ws.mkdir(parents=True, exist_ok=True)
    (ws / 'calc.py').write_text(DEBUG_SOURCE, encoding='utf-8')
    (ws / 'test_calc.py').write_text(DEBUG_TESTS, encoding='utf-8')
    started = time.monotonic()
    try:
        tid = m.submit('nim', DEBUG_GOAL, [], task_call(cfg),
                       checks=[{'type': 'command', 'command': 'python -m unittest -q test_calc',
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
        gate = sandbox.get_backend().run('python -m unittest -q test_calc', ws, 60, network=False)
        return row, elapsed, fixed, commands, gate
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
                row, elapsed, fixed, commands, gate = phase_debug(cfg, pathlib.Path(tmp))
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
